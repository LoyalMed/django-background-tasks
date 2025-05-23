# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import logging
import os
import sys
from datetime import datetime, timedelta
from multiprocessing.pool import ThreadPool

from django.db.utils import OperationalError
from django.utils import timezone
from six import python_2_unicode_compatible

from background_task import signals
from background_task.exceptions import BackgroundTaskError
from background_task.models import Task
from background_task.settings import app_settings

logger = logging.getLogger(__name__)


def bg_runner(proxy_task, task=None, *args, **kwargs):
    """
    Executes the function attached to task. Used to enable threads.
    If a Task instance is provided, args and kwargs are ignored and retrieved from the Task itself.
    """
    signals.task_started.send(Task)
    try:
        func = getattr(proxy_task, 'task_function', None)
        if isinstance(task, Task):
            args, kwargs = task.params()
        else:
            task_name = getattr(proxy_task, 'name', None)
            task_queue = getattr(proxy_task, 'queue', None)
            task_qs = Task.objects.get_task(task_name=task_name, args=args, kwargs=kwargs)
            if task_queue:
                task_qs = task_qs.filter(queue=task_queue)
            if task_qs:
                task = task_qs[0]
        if func is None:
            raise BackgroundTaskError("Function is None, can't execute!")
        func(*args, **kwargs)

        if task:
            # task done, so can delete it
            task.increment_attempts()
            completed = task.create_completed_task()
            signals.task_successful.send(sender=task.__class__, task_id=task.id, completed_task=completed)
            task.create_repetition()
            task.delete()
            logger.info('Ran task and deleting %s', task)

    except Exception as ex:
        t, e, traceback = sys.exc_info()
        if task:
            logger.error('Rescheduling %s', task, exc_info=(t, e, traceback))
            signals.task_error.send(sender=ex.__class__, task=task)
            task.reschedule(t, e, traceback)
        del traceback
    signals.task_finished.send(Task)


class PoolRunner:
    def __init__(self, bg_runner, num_processes):
        self._bg_runner = bg_runner
        self._num_processes = num_processes

    _pool_instance = None

    @property
    def _pool(self):
        if not self._pool_instance:
            self._pool_instance = ThreadPool(processes=self._num_processes)
        return self._pool_instance

    def run(self, proxy_task, task=None, *args, **kwargs):
        self._pool.apply_async(func=self._bg_runner, args=(proxy_task, task) + tuple(args), kwds=kwargs)

    __call__ = run


class Tasks(object):
    def __init__(self):
        self._tasks = {}
        self._runner = DBTaskRunner()
        self._task_proxy_class = TaskProxy
        self._bg_runner = bg_runner
        self._pool_runner = PoolRunner(bg_runner, app_settings.BACKGROUND_TASK_ASYNC_THREADS)

    def background(self, name=None, schedule=None, queue=None,
                   remove_existing_tasks=False):
        '''
        decorator to turn a regular function into
        something that gets run asynchronously in
        the background, at a later time
        '''

        # see if used as simple decorator
        # where first arg is the function to be decorated
        fn = None
        if name and callable(name):
            fn = name
            name = None

        def _decorator(fn):
            _name = name
            if not _name:
                _name = '%s.%s' % (fn.__module__, fn.__name__)
            proxy = self._task_proxy_class(_name, fn, schedule, queue,
                                           remove_existing_tasks, self._runner)
            self._tasks[_name] = proxy
            return proxy

        if fn:
            return _decorator(fn)

        return _decorator

    def run_task(self, task_name, args=None, kwargs=None):
        # task_name can be either the name of a task or a Task instance.
        if isinstance(task_name, Task):
            task = task_name
            task_name = task.task_name
            # When we have a Task instance we do not need args and kwargs, but
            # they are kept for backward compatibility
            args = []
            kwargs = {}
        else:
            task = None
        proxy_task = self._tasks[task_name]
        if app_settings.BACKGROUND_TASK_RUN_ASYNC:
            self._pool_runner(proxy_task, task, *args, **kwargs)
        else:
            self._bg_runner(proxy_task, task, *args, **kwargs)

    def run_next_task(self, queue=None):
        return self._runner.run_next_task(self, queue)


class TaskSchedule(object):
    SCHEDULE = 0
    RESCHEDULE_EXISTING = 1
    CHECK_EXISTING = 2

    def __init__(self, run_at=None, priority=None, action=None):
        self._run_at = run_at
        self._priority = priority
        self._action = action

    @classmethod
    def create(self, schedule):
        if isinstance(schedule, TaskSchedule):
            return schedule
        priority = None
        run_at = None
        action = None

        if schedule:
            if isinstance(schedule, (int, timedelta, datetime)):
                run_at = schedule
            else:
                run_at = schedule.get('run_at', None)
                priority = schedule.get('priority', None)
                action = schedule.get('action', None)

        return TaskSchedule(run_at=run_at, priority=priority, action=action)

    def merge(self, schedule):
        params = {}
        for name in ['run_at', 'priority', 'action']:
            attr_name = '_%s' % name
            value = getattr(self, attr_name, None)
            if value is None:
                params[name] = getattr(schedule, attr_name, None)
            else:
                params[name] = value
        return TaskSchedule(**params)

    @property
    def run_at(self):
        run_at = self._run_at or timezone.now()
        if isinstance(run_at, int):
            run_at = timezone.now() + timedelta(seconds=run_at)
        if isinstance(run_at, timedelta):
            run_at = timezone.now() + run_at
        return run_at

    @property
    def priority(self):
        return self._priority or 0

    @property
    def action(self):
        return self._action or TaskSchedule.SCHEDULE

    def __repr__(self):
        return 'TaskSchedule(run_at=%s, priority=%s)' % (self._run_at,
                                                         self._priority)

    def __eq__(self, other):
        return self._run_at == other._run_at \
            and self._priority == other._priority \
            and self._action == other._action


class DBTaskRunner(object):
    '''
    Encapsulate the model related logic in here, in case
    we want to support different queues in the future
    '''

    def __init__(self):
        self.worker_name = str(os.getpid())

    def schedule(self, task_name, args, kwargs, run_at=None,
                 priority=0, action=TaskSchedule.SCHEDULE, queue=None,
                 verbose_name=None, creator=None,
                 repeat=None, repeat_until=None, remove_existing_tasks=False):
        '''Simply create a task object in the database'''
        task = Task.objects.new_task(task_name, args, kwargs, run_at, priority,
                                     queue, verbose_name, creator, repeat,
                                     repeat_until, remove_existing_tasks)
        if action != TaskSchedule.SCHEDULE:
            task_hash = task.task_hash
            now = timezone.now()
            unlocked = Task.objects.unlocked(now)
            existing = unlocked.filter(task_hash=task_hash)
            if queue:
                existing = existing.filter(queue=queue)
            if action == TaskSchedule.RESCHEDULE_EXISTING:
                updated = existing.update(run_at=run_at, priority=priority)
                if updated:
                    return
            elif action == TaskSchedule.CHECK_EXISTING:
                if existing.count():
                    return

        task.save()
        signals.task_created.send(sender=self.__class__, task=task)
        return task

    def get_task_to_run(self, tasks, queue=None):
        try:
            available_tasks = [task for task in Task.objects.find_available(queue)
                               if task.task_name in tasks._tasks][:5]
            for task in available_tasks:
                # try to lock task
                locked_task = task.lock(self.worker_name)
                if locked_task:
                    return locked_task
            return None
        except OperationalError:
            logger.warning('Failed to retrieve tasks. Database unreachable.')

    def run_task(self, tasks, task):
        logger.info('Running %s', task)
        tasks.run_task(task)

    def run_next_task(self, tasks, queue=None):
        task = self.get_task_to_run(tasks, queue)
        if task:
            self.run_task(tasks, task)
            return True
        else:
            return False


@python_2_unicode_compatible
class TaskProxy(object):
    def __init__(self, name, task_function, schedule, queue, remove_existing_tasks, runner):
        self.name = name
        self.now = self.task_function = task_function
        self.runner = runner
        self.schedule = TaskSchedule.create(schedule)
        self.queue = queue
        self.remove_existing_tasks = remove_existing_tasks

    def __call__(self, *args, **kwargs):
        schedule = kwargs.pop('schedule', None)
        schedule = TaskSchedule.create(schedule).merge(self.schedule)
        run_at = schedule.run_at
        priority = kwargs.pop('priority', schedule.priority)
        action = schedule.action
        queue = kwargs.pop('queue', self.queue)
        verbose_name = kwargs.pop('verbose_name', None)
        creator = kwargs.pop('creator', None)
        repeat = kwargs.pop('repeat', None)
        repeat_until = kwargs.pop('repeat_until', None)
        remove_existing_tasks = kwargs.pop('remove_existing_tasks', self.remove_existing_tasks)

        return self.runner.schedule(self.name, args, kwargs, run_at, priority,
                                    action, queue, verbose_name, creator,
                                    repeat, repeat_until,
                                    remove_existing_tasks)

    def __str__(self):
        return 'TaskProxy(%s)' % self.name


tasks = Tasks()


def autodiscover():
    """
    Autodiscover tasks.py files in much the same way as admin app
    """
    from django.conf import settings
    from django.utils.module_loading import import_module

    installed_apps = settings.INSTALLED_APPS.copy()
    if 'report_builder' in installed_apps:
        installed_apps.remove('report_builder')

    for app in installed_apps:
        try:
            import_module("%s.tasks" % app)
        except ImportError:
            continue
