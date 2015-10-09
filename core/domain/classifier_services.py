# coding: utf-8
#
# Copyright 2015 The Oppia Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import operator

import numpy


class StringClassifier(object):
    """A classifier that uses supervised learning to match free-form text

    answers to answer groups. The classifier trains on answers that exploration
    editors have assigned to an answer group. Given a new answer, it predicts
    the answer group using Latent Dirichlet Allocation
    (https://en.wikipedia.org/wiki/Latent_Dirichlet_allocation) with Gibbs
    Sampling.

    Below is an example workflow for running a batch job that builds a new
    classifier and outputs it to a dictionary.

        # Examples are formatted as a list. Each element is a doc followed by a
        # list of labels.
        examples = [
            ['i eat fish and vegetables', ['food']],
            ['fish are pets', ['pets']],
            ['my kitten eats fish', ['food', 'pets']]
        ]
        string_classifier = StringClassifier()
        string_classifier.load_examples(examples)
        classifier_dict = string_classifier.to_dict()
        save_to_data_store(classifier_dict)

    Below is an example workflow for using an existing classifier to predict a
    document's label.

        # A list of docs to classify.
        prediction_examples = [
            'i only eat fish and vegetables'
        ]
        classifier_dict = load_from_data_store()
        string_classifier = StringClassifier()
        string_classifier.from_dict(classifier_dict)
        doc_ids = string_classifier.add_examples_for_predicting(
            prediction_examples)
        label = string_classifier.predict_label_for_doc(doc_ids[0])
        print label

    Below are some concepts used in this class.
    doc - A student free form response, represented as a string of arbitrary
        non-whitespace characters (a "word") separated by single spaces.
    word - A string of arbitrary non-whitespace characters.
    word id - A unique word. Each unique word has one word id that is used
        to represent the word in the classifier model.
    word instance - An instance of a word id. Each word id can have multiple
        instances corresponding to its occurrences in all docs.
    label - An answer group that the doc should correspond to. If a doc is
        being added to train a model, labels are provided. If a doc is being
        added for prediction purposes, no labels are provided. If a doc does
        not match any label, the doc should have only one label, '_default'.
    label bit vector - A bit vector corresponding to a doc, indexed by label
        id. A one in the vector means the label id matches some word instance
        in the doc; a zero in the vector means the label id does not match any
        word instance in the doc.

    This class uses index notation with the format "_V_XY", where V is the
    element of an array, and X and Y are the indices used to measure V.
    https://en.wikipedia.org/wiki/Index_notation

    The following maps index notation letters to their meanings:
    b - boolean value for whether Y is set in X
    c - count of Y's in X
    p - position of a value V in X
    l - label id
    w - word id
    d - doc id

    Internal model representation:
    _w_dp - lists of word ids, where each list represents a doc
    _b_dl - lists of booleans, where each boolean represents whether a
        label is contained within a doc.
    _l_dp - lists of label ids, where each list represents what label
        is assigned to a word instance.
    _c_dl - lists of counts, where each list represents the number of
        word instances assigned to each label in a doc.
    _c_lw - lists of counts, where each list represents the number of
        word instances assigned to each label for a given word id (aggregated
        across all docs).
    _c_l - a list of counts, where each count is the number of times
        a label was assigned (aggregated over all word instances in all docs).

    There are two internal learning rates, _DEFAULT_ALPHA and _DEFAULT_BETA.
    These are initialized to Wikipedia's recommendations. Do not change these
    unless you know what you're doing.

    It is possible for a word instance in a doc to not have an explicit label
    assigned to it. This is characterized by assigning _DEFAULT_LABEL to the
    word instance."""

    _DEFAULT_ALPHA = 0.1
    _DEFAULT_BETA = 0.001

    _DEFAULT_TRAINING_ITERATIONS = 25
    _DEFAULT_PREDICTION_ITERATIONS = 5

    _DEFAULT_PREDICTION_THRESHOLD = 0.5

    _DEFAULT_LABEL = '_default'

    def __init__(self):
        """ Initializes constants for the classifier. Setting a seed ensures

        that results are deterministic. There is nothing special about the
        value 4. These should not be changed unless you know what you're
        doing."""
        numpy.random.seed(seed=4)

        self._alpha = self._DEFAULT_ALPHA
        self._beta = self._DEFAULT_BETA

        self._training_iterations = self._DEFAULT_TRAINING_ITERATIONS
        self._prediction_iterations = self._DEFAULT_PREDICTION_ITERATIONS

        self._prediction_threshold = self._DEFAULT_PREDICTION_THRESHOLD

    def _get_word_id(self, word):
        """Returns a word's id if it exists, otherwise assigns

        a new id to the word and returns it."""
        if word not in self._word_to_id:
            self._word_to_id[word] = self._num_words
            self._num_words += 1
        return self._word_to_id[word]

    def _get_label_id(self, label):
        """Returns a label's id if it exists, otherwise assigns

        a new id to the label and returns it."""
        if label not in self._label_to_id:
            self._label_to_id[label] = self._num_labels
            self._num_labels += 1
        return self._label_to_id[label]

    def _get_label_name(self, l):
        """Returns a label's string name given its internal id.

        If the id does not have a corresponding name, an exception is
        raised."""
        for label_name, label_id in self._label_to_id.iteritems():
            if label_id == l:
                return label_name

        raise Exception('Label id %d does not exist.' % l)

    def _get_doc_with_label_vector(self, d):
        """Given a doc id, return the doc and its label bit vector."""
        return self._w_dp[d], self._b_dl[d]

    def _label_set_to_vector(self, labels):
        """Generate and return a label bit vector given a list of labels that
        are turned on for the vector."""
        label_vector = numpy.zeros(self._num_labels)
        for label in labels:
            label_vector[self._get_label_id(label)] = 1
        label_vector[self._label_to_id[self._DEFAULT_LABEL]] = 1
        return label_vector

    def _get_label_vector(self, labels):
        """Returns a label bit vector given a list of labels.

        An empty label list is an alias for all labels being set. This is
        useful because no old input data needs to be altered when a new label
        is introduced to the model."""
        if len(labels) == 0:
            return numpy.ones(self._num_labels)
        return self._label_set_to_vector(labels)

    def _validate_training_label_list(self, label_list):
        """Checks that a label list is not empty. Used when examples are added

        for training purposes. This is because empty label lists are treated as
        a doc having all labels."""
        if len(label_list) == 0:
            raise Exception(
                'A document\'s labels cannot be empty when training.')

    def _update_counting_matrices(self, d, w, l, val):
        """Updates counting matrices (ones that begin with _c) when a label

        is assigned and unassigned to a word."""
        self._c_dl[d, l] += val
        self._c_lw[l, w] += val
        self._c_l[l] += val

    def _increment_counting_matrices(self, d, w, l):
        """Updates counting matrices when a label is assigned to a word

        instancein a doc."""
        self._update_counting_matrices(d, w, l, 1)

    def _decrement_counting_matrices(self, d, w, l):
        """Updates counting matrices when a label is unassigned from a word

        instance in a doc."""
        self._update_counting_matrices(d, w, l, -1)

    def _run_gibbs_sampling(self, doc_ids):
        """Runs one iteration of Gibbs sampling on the provided docs.

        The statez variable is used for debugging, and possibly convergence
        testing in the future."""
        if doc_ids is None:
            doc_ids = xrange(self._num_docs)

        statez = {
            'updates': 0,
            'computes': 0
        }

        for d in doc_ids:
            doc, labels = self._get_doc_with_label_vector(d)
            for p, w in enumerate(doc):
                l = self._l_dp[d][p]

                self._decrement_counting_matrices(d, w, l)

                coeff_a = 1.0 / (
                    self._c_dl[d].sum() + self._num_labels * self._alpha)
                coeff_b = 1.0 / (
                    self._c_lw.sum(axis=1) + self._num_words * self._beta)
                label_probabilities = (
                    labels *
                    coeff_a * (self._c_dl[d] + self._alpha) *
                    coeff_b * (self._c_lw[:, w] + self._beta))
                new_label = numpy.random.multinomial(
                    1,
                    label_probabilities / label_probabilities.sum()).argmax()

                statez['computes'] += 1
                if l != new_label:
                    statez['updates'] += 1

                self._l_dp[d][p] = new_label
                self._increment_counting_matrices(d, w, new_label)

        return statez

    def _get_label_probabilities(self, d):
        """Returns a list of label probabilities for a given doc, indexed by

        label id."""
        label_probabilities = self._c_dl[d] + (self._b_dl[d] * self._alpha)
        label_probabilities = (label_probabilities /
            label_probabilities.sum(axis=0)[numpy.newaxis])
        return label_probabilities

    def _get_prediction_report_for_doc(self, d):
        """Generates and returns a prediction report for a given doc.

        The prediction report is a dict with the following keys:
        - 'prediction_label_id': the document's predicted label id
        - 'prediction_label_name': prediction_label_id's label name
        - 'prediction_confidence': the prediction confidence. This is
        Prob(the doc should be assigned this label |
        the doc is not assigned _DEFAULT_LABEL).
        - 'all_predictions': a dict mapping each label to
        Prob(the doc should be assigned to this label).

        Because _DEFAULT_LABEL is a special label that captures unspecial
        tokens (how ironic), its probability is not a good predicting
        indicator. For the current normalization process, it has on
        average a higher probability than other labels. To combat this,
        all other labels are normalized as if the default label did not
        exist. For example, if we have two labels with probability 0.2
        and 0.3 with the default label having probability 0.5, the
        normalized probability for the two labels without the default
        label is 0.4 and 0.6, respectively.

        The prediction threshold is currently defined at the classifier level.
        A higher prediction threshold indicates that the predictor needs
        more confidence prior to making a prediction, otherwise it will
        predict _DEFAULT_LABEL. This will make non-default predictions more
        accurate, but result in fewer of them."""
        default_label_id = self._get_label_id(self._DEFAULT_LABEL)
        prediction_label_id = default_label_id
        prediction_confidence = 0
        label_probabilities = self._get_label_probabilities(d)
        normalization_coeff = (1.0 /
            (1.0 - label_probabilities[default_label_id]))

        for l, prob in enumerate(label_probabilities):
            if (l != default_label_id and
                    prob * normalization_coeff > self._prediction_threshold
                    and prob * normalization_coeff > prediction_confidence):
                prediction_label_id = l
                prediction_confidence = prob * normalization_coeff

        return {
            'prediction_label_id': prediction_label_id,
            'prediction_label_name':
                self._get_label_name(prediction_label_id),
            'prediction_confidence': prediction_confidence,
            'all_predictions': label_probabilities
        }

    def _parse_examples(self, examples):
        """Unzips docs and label lists from examples and returns the two lists.

        Docs are split on whitespace. Order is preserved."""
        docs = []
        labels_list = []
        for example in examples:
            doc_string = example[0]
            doc = doc_string.split()
            if len(doc) > 0:
                labels = example[1]
                docs.append(doc)
                labels_list.append(labels)
        return docs, labels_list

    def _iterate_gibbs_sampling(self, iterations, doc_ids=None):
        """Runs Gibbs sampling for "iterations" number of times on the provided

        docs."""
        if doc_ids is None:
            doc_ids = xrange(self._num_docs)

        for i in xrange(iterations):
            statez = self._run_gibbs_sampling(doc_ids)

    def _add_examples(self, examples, iterations):
        """Adds examples to the internal state of the classifier, assigns

        random initial labels to only the added docs, and runs Gibbs sampling
        for iterations number of iterations."""
        if len(examples) == 0:
            return

        docs, labels_list = self._parse_examples(examples)

        last_num_labels = self._num_labels
        last_num_docs = self._num_docs
        last_num_words = self._num_words

        # Increments _num_labels with any new labels
        [map(self._get_label_id, labels) for labels in labels_list]
        self._num_docs += len(docs)

        self._b_dl = numpy.concatenate(
            (self._b_dl, numpy.zeros(
                (last_num_docs, self._num_labels - last_num_labels),
                dtype=int)), axis=1)
        self._b_dl = numpy.concatenate(
            (self._b_dl, [
                self._get_label_vector(labels) for labels in labels_list
            ]), axis=0)
        self._w_dp.extend([map(self._get_word_id, doc) for doc in docs])
        self._c_dl = numpy.concatenate(
            (self._c_dl, numpy.zeros(
                (last_num_docs, self._num_labels - last_num_labels),
                dtype=int)), axis=1)
        self._c_dl = numpy.concatenate(
            (self._c_dl, numpy.zeros(
                (self._num_docs - last_num_docs, self._num_labels),
                dtype=int)), axis=0)
        self._c_lw = numpy.concatenate(
            (self._c_lw, numpy.zeros(
                (last_num_labels, self._num_words - last_num_words),
                dtype=int)), axis=1)
        self._c_lw = numpy.concatenate(
            (self._c_lw, numpy.zeros(
                (self._num_labels - last_num_labels, self._num_words),
                dtype=int)), axis=0)
        self._c_l = numpy.concatenate(
            (self._c_l, numpy.zeros(
                self._num_labels - last_num_labels, dtype=int)))

        for d in xrange(last_num_docs, self._num_docs):
            doc, _ = self._get_doc_with_label_vector(d)
            l_p = numpy.random.random_integers(
                0, self._num_labels - 1, size=len(doc)).tolist()
            self._l_dp.append(l_p)
            for w, l in zip(doc, l_p):
                self._increment_counting_matrices(d, w, l)
            self._iterate_gibbs_sampling(iterations, [d])

        return xrange(last_num_docs, self._num_docs)

    def add_examples_for_training(self, training_examples):
        """Adds examples to the classifier with _training_iterations number of

        iterations."""
        for doc, label_list in training_examples:
            self._validate_training_label_list(label_list)
        return self._add_examples(training_examples, self._training_iterations)

    def add_examples_for_predicting(self, prediction_examples):
        """Adds examples to the classifier with _prediction_iterations number

        of iterations."""
        return self._add_examples(
            zip(prediction_examples, [[] for _ in prediction_examples]),
            self._prediction_iterations)

    def load_examples(self, examples):
        """Sets the internal state of the classifier, assigns random initial

        labels to the docs, and runs Gibbs sampling for _training_iterations
        number of iterations."""
        docs, labels_list = self._parse_examples(examples)

        label_set = set(
            [self._DEFAULT_LABEL] +
            [label for labels in labels_list for label in labels])

        self._num_labels = len(label_set)
        self._label_to_id = dict(zip(label_set, xrange(self._num_labels)))

        self._num_words = 0
        self._word_to_id = {}

        self._num_docs = len(docs)

        self._b_dl = numpy.array(
            map(self._get_label_vector, labels_list), dtype=int)
        self._w_dp = [map(self._get_word_id, doc) for doc in docs]
        self._c_dl = numpy.zeros(
            (self._num_docs, self._num_labels), dtype=int)
        self._c_lw = numpy.zeros(
            (self._num_labels, self._num_words), dtype=int)
        self._c_l = numpy.zeros(self._num_labels, dtype=int)
        self._l_dp = []

        for d in xrange(self._num_docs):
            doc, _ = self._get_doc_with_label_vector(d)
            l_p = numpy.random.random_integers(
                0, self._num_labels - 1, size=len(doc)).tolist()
            self._l_dp.append(l_p)
            for w, l in zip(doc, l_p):
                self._increment_counting_matrices(d, w, l)
        self._iterate_gibbs_sampling(self._training_iterations)

    def predict_label_for_doc(self, d):
        """Returns the predicted label from a doc's prediction report."""
        return self._get_prediction_report_for_doc(d)['prediction_label_name']

    def to_dict(self):
        """Converts a classifier into a dict model."""
        model = {}
        model['_alpha'] = copy.deepcopy(self._alpha)
        model['_beta'] = copy.deepcopy(self._beta)
        model['_prediction_threshold'] = copy.deepcopy(
            self._prediction_threshold)
        model['_training_iterations'] = copy.deepcopy(
            self._training_iterations)
        model['_prediction_iterations'] = copy.deepcopy(
            self._prediction_iterations)
        model['_num_labels'] = copy.deepcopy(self._num_labels)
        model['_num_docs'] = copy.deepcopy(self._num_docs)
        model['_num_words'] = copy.deepcopy(self._num_words)
        model['_label_to_id'] = copy.deepcopy(self._label_to_id)
        model['_word_to_id'] = copy.deepcopy(self._word_to_id)
        model['_w_dp'] = copy.deepcopy(self._w_dp)
        model['_b_dl'] = copy.deepcopy(self._b_dl)
        model['_l_dp'] = copy.deepcopy(self._l_dp)
        model['_c_dl'] = copy.deepcopy(self._c_dl)
        model['_c_lw'] = copy.deepcopy(self._c_lw)
        model['_c_l'] = copy.deepcopy(self._c_l)
        return model

    def from_dict(self, model):
        """Converts a dict model into a classifier."""
        self._alpha = copy.deepcopy(model['_alpha'])
        self._beta = copy.deepcopy(model['_beta'])
        self._prediction_threshold = copy.deepcopy(
            model['_prediction_threshold'])
        self._training_iterations = copy.deepcopy(
            model['_training_iterations'])
        self._prediction_iterations = copy.deepcopy(
            model['_prediction_iterations'])
        self._num_labels = copy.deepcopy(model['_num_labels'])
        self._num_docs = copy.deepcopy(model['_num_docs'])
        self._num_words = copy.deepcopy(model['_num_words'])
        self._label_to_id = copy.deepcopy(model['_label_to_id'])
        self._word_to_id = copy.deepcopy(model['_word_to_id'])
        self._w_dp = copy.deepcopy(model['_w_dp'])
        self._b_dl = copy.deepcopy(model['_b_dl'])
        self._l_dp = copy.deepcopy(model['_l_dp'])
        self._c_dl = copy.deepcopy(model['_c_dl'])
        self._c_lw = copy.deepcopy(model['_c_lw'])
        self._c_l = copy.deepcopy(model['_c_l'])
