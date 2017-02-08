from __future__ import absolute_import, division, print_function

from collections import deque
from datetime import datetime
from Queue import Queue

import e3.log
from e3.collection.dag import DAGIterator

logger = e3.log.getLogger('e3.job.scheduler')


class Scheduler(object):
    """Handle parallel execution of interdependent jobs."""

    def __init__(self,
                 job_provider,
                 collect=None,
                 queues=None,
                 tokens=1):
        """Initialize Scheduler.

        :param job_provider: function that returns instances of Job.
            The function takes as arguments: the job uid, the data
            associated with it, the list of predecessors id and and
            a notification function called when the job end.
        :type job_provider: (str, T, list[str], str -> None) -> Job
        :param collect: function that collect results from the
            jobs. If the function returns True then the job is requeued
        :type collect: Job -> bool
        :param queues: describes the list of queues handled by the
            scheduler. The format is a dictionary for which which
            keys are queue names and value the max number of tokens
            available at a given time. if None then a single queue
            called "default" is created. Its size is then given by
            the tokens parameter.
        :type queues: dict(str, int) | None
        :param tokens: number of tokens for the default queue. Relevant
            only when queues is None
        :param tokens: int
        """
        self.job_provider = job_provider
        self.collect = collect
        if self.collect is None:
            self.collect = lambda x: False

        # Initialize named queues
        self.queues = {}
        self.tokens = {}
        self.n_tokens = 0

        if queues is None:
            # If no queues are specificied create a default one
            # with no name.
            queues = {'default': tokens}

        # Create the queues
        for name, max_token in queues.iteritems():
            self.queues[name] = deque()
            self.tokens[name] = max_token
            self.n_tokens += max_token

        # Create a slot reserve. The goal is to give a given job a number
        # which is unique among the active jobs. We need a maximum of
        # self.n_tokens slots
        self.slots = range(self.n_tokens)

    @classmethod
    def simple_provider(cls, job_class):
        """Return a simple provider based on a given Job class.

        :param job_class: a subclass of Job
        :type job_class: T
        """
        def provider(uid, data, predecessors, notify_end):
            return job_class(uid, data, notify_end)
        return provider

    def init_state(self, dag):
        """Reinitialize the scheduler state (internal function).

        :param dag: the dag representing the list of job to execute
        :type dag: e3.collection.dag
        """
        # Active jobs
        self.active_jobs = []

        # Total number of jobs in the queues
        self.queued_jobs = 0

        # Have all jobs been queued?
        self.all_jobs_queued = False

        # Message queue to get job end notifications
        self.message_queue = Queue()

        self.dag_iterator = DAGIterator(dag, enable_busy_state=True)
        self.start_time = datetime.now()
        self.stop_time = None
        self.max_active_jobs = 0

    @property
    def is_finished(self):
        """Check if all jobs have been executed (internal).

        :return: True if complete
        :rtype: bool
        """
        # The run is considered finished once there is no more job
        # in the DAG, the queues and that no job is running.
        return self.all_jobs_queued and \
            self.queued_jobs == 0 and \
            not self.active_jobs

    def log_state(self):
        """Log the current state of the scheduler (internal)."""
        logger.debug('non-ready?: %s, in queue: %s, running: %s',
                     not self.all_jobs_queued,
                     self.queued_jobs,
                     len(self.active_jobs))

    def run(self, dag):
        """Launch the scheduler.

        :param dag: jobs to be executed
        :type dag: e3.collection.dag
        """
        self.init_state(dag)

        while not self.is_finished:
            self.enqueue()
            self.launch()
            self.log_state()
            self.max_active_jobs = max(self.max_active_jobs,
                                       len(self.active_jobs))
            self.wait()
        self.stop_time = datetime.now()

    def enqueue(self):
        """Push into the queues job that are ready (internal)."""
        if self.all_jobs_queued:
            return

        try:
            while True:
                uid, data, predecessors = \
                    self.dag_iterator.next_element()
                if uid is None:
                    # No more jobs ready
                    return

                job = self.job_provider(
                    uid,
                    data,
                    predecessors=predecessors,
                    notify_end=lambda x: self.message_queue.put(x))
                if job.should_skip:
                    self.collect(job)
                    self.dag_iterator.leave(uid)
                else:
                    self.queues[job.queue_name].append(job)
                    self.queued_jobs += 1
        except StopIteration:
            self.all_jobs_queued = True

    def launch(self):
        """Launch next jobs in the queues (internal)."""
        if self.queued_jobs == 0:
            return

        for name in self.queues:
            q = self.queues[name]
            while q and q[0].tokens <= self.tokens[name]:
                next_job = q.popleft()
                next_job.start(slot=self.slots.pop())
                self.tokens[name] -= next_job.tokens
                self.queued_jobs -= 1
                self.active_jobs.append(next_job)

    def wait(self):
        """Wait for the end of an active job."""
        if not self.active_jobs:
            return

        # Wait for message from one the active jobs
        uid = self.message_queue.get(True, None)
        job_index, job = next(((index, job)
                               for index, job in enumerate(self.active_jobs)
                               if job.uid == uid))
        self.slots.append(job.slot)

        # Liberate the resources taken by the job
        self.tokens[job.queue_name] += job.tokens

        if self.collect(job):
            # Requeue when needed
            self.queues[job.queue_name].append(job)
            self.queued_jobs += 1
        else:
            # Mark the job as completed
            self.dag_iterator.leave(job.uid)

        del self.active_jobs[job_index]
