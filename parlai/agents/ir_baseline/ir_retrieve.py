# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree. An additional grant
# of patent rights can be found in the PATENTS file in the same directory.
"""a string match retriever."""

import asyncio
import copy
import logging
from multiprocessing import Lock, Queue, Semaphore, Value
import numpy as np
import os
import scipy.sparse as sp
import sqlite3

try:
    from drqa import tokenizers
    from drqa.retriever import utils
except ImportError:
    raise RuntimeError("DrQA needs to be installed for using the retriever.")

from parlai.core.agents import Agent


class StringMatchRetrieverAgent(Agent):
    """Builds and/or loads a string match retriever

    Input document is saved to <retriever_database> as a DB table <fact_id, fact>:
        - fact_id INT: the unique identifier
        - fact STRING: the fact string
    Frequency (sparse) matrix is saved to <retriever-file>.
    Token to id mapping is saved to <retriever_tokens>.

    The retrieve() function outputs facts sorted by tfidfs.
    """
    END_OF_DATA = "EOD"
    DEFAULT_MAX_FACTS = 100000
    DOC_TABLE_NAME = 'document'
    FACT_QUERY = (
        "INSERT INTO %s(fact_id, fact) VALUES(?,?)" %
        DOC_TABLE_NAME
    )
    TFIDFS_TABLE_NAME = "tfidfs"

    def print_info(self, msg):
        additional_info = "-- Process %d" % self.child_id if hasattr(self, 'child_id') else ""
        logging.info(("[ StringMatchRetriever %s]: " + str(msg))
                      % additional_info
        )

    @staticmethod
    def add_cmdline_args(argparser):
        retriever = argparser.add_argument_group('Retriever Arguments')
        retriever.add_argument(
            '--retriever-file',
            help='if set, the retriever will save to this file as default',
        )
        retriever.add_argument(
            '--retriever-maxexs',
            default=StringMatchRetrieverAgent.DEFAULT_MAX_FACTS,
            type=int,
            help='max number of examples to build retriever on; input 0 if no limit.',
        )
        retriever.add_argument(
            '--retriever-database',
            help='if set, the input data will save to this file as default',
        )
        retriever.add_argument(
            '--retriever-tokens',
            help='if set, the tokens will save to this file as default',
        )

    def __init__(self, opt, shared=None):
        super().__init__(opt)
        self.id = 'StringMatchRetrieverAgent'
        self.retriever_file = opt.get('retriever_file')
        self.doc_file = opt.get(
            'retriever_database',
            '_' + self.retriever_file + '.db',
        )
        self.token_file = opt.get(
            'retriever_tokens',
            '_' + self.retriever_file + '.npy',
        )
        self.all_tokens = {}
        self._init_fact_table()
        self.tokenizer = tokenizers.get_class(opt.get('tokenizer', 'simple'))()
        self.freq_token = []
        self.freq_fact_id = []
        self.freq_freq = []
        self.cnt = 0
        self.load()
        if shared:
            self.fact_id = shared['fact_id']
            self.db_lock = shared['db_lock']
            self.comm_queue = shared['comm_queue']
            self.child_id = shared['child_id']
        else:
            self.fact_id = Value('i', 0)
            self.db_lock = Lock()
            self.comm_queues = []

    def _print_db(self):
        for row in self.cursor.execute('select * from %s' % self.DOC_TABLE_NAME):
            print(row)

    def get_num_docs(self):
        if not hasattr(self, 'num_docs'):
            self.num_docs = self.fact_id.value
        return self.num_docs

    def get_num_tokens(self):
        if not hasattr(self, 'num_tokens'):
            self.num_tokens =  len(self.all_tokens)
        return self.num_tokens

    def retrieve(self, query, max_results=100):
        tokens = [token for token in self.tokenize(query) if token in self.all_tokens]
        if not tokens:
            return
        self.compute_tfidf()
        wids = [self.token2id(token) for token in tokens]
        # Count TF
        wids_unique, wids_counts = np.unique(wids, return_counts=True)
        tfs = np.log1p(wids_counts)
        # Count IDF
        Ns = self.doc_freqs[wids_unique]
        idfs = np.log((self.get_num_docs() - Ns + 0.5) / (Ns + 0.5))
        idfs[idfs < 0] = 0
        # TF-IDF
        data = np.multiply(tfs, idfs)
        # One row, sparse csr matrix
        indptr = np.array([0, len(wids_unique)])
        spvec = sp.csr_matrix(
            (data, wids_unique, indptr), shape=(1, self.get_num_tokens())
        )
        res = spvec * self.tfidfs
        if len(res.data) <= max_results:
            o_sort = np.argsort(-res.data)
        else:
            o = np.argpartition(-res.data, max_results)[0:max_results]
            o_sort = o[np.argsort(-res.data[o])]
        doc_scores = res.data[o_sort]
        doc_ids = res.indices[o_sort]
        for _doc_id in doc_ids:
            self.cursor.execute(
                "SELECT fact FROM %s WHERE fact_id=?" % self.DOC_TABLE_NAME,
                (str(_doc_id),),
            )
            yield self.cursor.fetchall()[0][0]

    def share(self):
        shared = super().share()
        shared['fact_id'] = self.fact_id
        shared['db_lock'] = self.db_lock
        shared['child_id'] = len(self.comm_queues)
        self.comm_queues.append(Queue())
        shared['comm_queue'] = self.comm_queues[-1]
        return shared

    def shutdown(self):
        if hasattr(self, "comm_queue"):
            # workder: send task ids, freqs to master
            self.print_info("start sending token ids...")
            for _task, _task_id in self.all_tokens.items():
                self.comm_queue.put((_task, _task_id))
            self.print_info("start sending freqs...")
            for ind in range(len(self.freq_fact_id)):
                self.comm_queue.put((
                    self.freq_token[ind],
                    self.freq_fact_id[ind],
                    self.freq_freq[ind],
                ))
            self.comm_queue.put(self.END_OF_DATA)
            self.print_info("all data send.")
        else:
            # master: collect data and update
            self.process_data_finished = [False] * len(self.comm_queues)
            self.finished_cnt = 0
            self.child_token_map = [dict() for _ in self.comm_queues]
            asyncio.get_event_loop().run_until_complete(self.process_child_data())
            self.save()

    async def process_child_data(self):
        num_queues = len(self.comm_queues)
        while self.finished_cnt != num_queues:
            for queue_ind in range(num_queues):
                await (self._process_data(queue_ind))

    async def _process_data(self, queue_ind):
        if self.process_data_finished[queue_ind]:
            return
        data = self.comm_queues[queue_ind].get()
        if data == self.END_OF_DATA:
            self.process_data_finished[queue_ind] = True
            self.finished_cnt += 1
        elif len(data) == 2:
            (_token, _token_id) = data
            _true_token_id = self.token2id(_token)
            self.child_token_map[queue_ind][_token_id] = _true_token_id
        elif len(data) == 3:
            (_token_id, _fact_id, _freq) = data
            self.new_freq(
                self.child_token_map[queue_ind][_token_id],
                _fact_id,
                _freq,
            )
        else:
            raise RuntimeError("StringMatchRetrieverAgent: wrong data format send from child to master.")

    def _init_fact_table(self):
        is_new_table = not os.path.isfile(self.doc_file)
        try:
            doc_conn = sqlite3.connect(self.doc_file)
        except sqlite3.Error:
            raise RuntimeError("Unable to access DB file '%s'" % self.doc_file)
        doc_conn.execute("PRAGMA journal_mode=WAL")
        doc_conn.execute("PRAGMA busy_timeout=60000")
        if is_new_table:
            doc_cursor = doc_conn.cursor()
            doc_cursor.execute(
                "CREATE TABLE %s (fact_id INTEGER PRIMARY KEY, fact) WITHOUT ROWID"
                % StringMatchRetrieverAgent.DOC_TABLE_NAME
            )
        self.cursor = doc_conn.cursor()

    def load(self):
        if not os.path.isfile(self.retriever_file):
            return
        self.all_tokens = np.load(self.token_file).item()
        self.count_matrix = sp.load_npz(self.retriever_file)
        (self.num_tokens, self.num_docs) = np.shape(self.count_matrix)
        self._compute_tfidf_from_count_matrix(self.count_matrix)

    def save(self):
        np.save(self.token_file, self.all_tokens)
        sp.save_npz(self.retriever_file, self._compute_count_matrix())

    def act(self):
        # reset tfidfs matrix
        if hasattr(self, 'tfidfs'):
            del self.tfidfs
            del self.count_matrix
            del self.doc_freqs
            del self.num_docs
            del self.num_tokens
        if 'text' not in self.observation:
            logging.warning("observation: %s has no 'text' field, skipped." % str(self.observation))
            return {'id': 'Retriever'}
        self.cnt += 1
        # report progress
        if self.cnt % 10000 == 0:
            self.print_info("Processed %d rows..." % self.cnt)
        self.process_act(self.observation.get('text'))
        return {'id': 'Retriever'}

    def insert_fact_without_id(self, fact):
        new_fact_id = self.new_fact_id()
        with self.db_lock:
            with self.cursor.connection:
                self.cursor.execute(self.FACT_QUERY, (new_fact_id, fact,))
        return new_fact_id

    def tokenize(self, query):
        tokens = self.tokenizer.tokenize(utils.normalize(query))
        return tokens.ngrams(n=1, uncased=True,
                             filter_fn=utils.filter_ngram)

    def process_act(self, fact):
        fact_id = self.insert_fact_without_id(fact)
        unique_tokens, tokens_cnts = np.unique(self.tokenize(fact), return_counts=True)
        for ind in range(len(unique_tokens)):
            _token = unique_tokens[ind]
            _freq = tokens_cnts[ind]
            self.new_freq(_token, fact_id, _freq)

    def new_freq(self, token, fact_id, freq):
        self.freq_token.append(self.token2id(token))
        self.freq_fact_id.append(fact_id)
        self.freq_freq.append(freq)

    def get_doc_freqs(self, cnts):
        if not hasattr(self, 'doc_freqs'):
            binary = (cnts > 0).astype(int)
            freqs = np.array(binary.sum(1)).squeeze()
            self.doc_freqs = freqs
        return self.doc_freqs

    def _compute_count_matrix(self):
        if not hasattr(self, 'count_matrix'):
            count_matrix = sp.csr_matrix(
                (self.freq_freq, (self.freq_token, self.freq_fact_id)),
                shape=(self.get_num_tokens(), self.get_num_docs())
            )
            count_matrix.sum_duplicates()
            self.count_matrix = count_matrix
        return self.count_matrix

    def compute_tfidf(self):
            return self._compute_tfidf_from_count_matrix(self._compute_count_matrix())

    def _compute_tfidf_from_count_matrix(self, count_matrix):
        if not hasattr(self, 'tfidfs'):
            Ns = self.get_doc_freqs(count_matrix)
            idfs = np.log((count_matrix.shape[1] - Ns + 0.5) / (Ns + 0.5))
            idfs[idfs < 0] = 0
            idfs = sp.diags(idfs, 0)
            tfs = count_matrix.log1p()
            self.tfidfs = idfs.dot(tfs)
        return self.tfidfs

    def token2id(self, token):
        if isinstance(token, int):
            return token
        if token not in self.all_tokens:
            token_id = len(self.all_tokens)
            self.all_tokens[token] = token_id
        return self.all_tokens[token]

    def new_fact_id(self):
        with self.fact_id.get_lock():
            cur_id = self.fact_id.value
            self.fact_id.value += 1
        return cur_id
