# Copyright 2013: Mirantis Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import collections
import copy
import json
import threading
import time
import traceback

import jsonschema
from oslo_config import cfg

from rally.common.i18n import _
from rally.common import logging
from rally.common import objects
from rally.common import utils
from rally import consts
from rally import exceptions
from rally.task import context
from rally.task import hook
from rally.task import runner
from rally.task import scenario
from rally.task import sla
from rally.task import trigger


LOG = logging.getLogger(__name__)

CONF = cfg.CONF

TASK_ENGINE_OPTS = [
    cfg.IntOpt("raw_result_chunk_size", default=1000, min=1,
               help="Size of raw result chunk in iterations"),
]
CONF.register_opts(TASK_ENGINE_OPTS)


class ResultConsumer(object):
    """ResultConsumer class stores results from ScenarioRunner, checks SLA.

    Also ResultConsumer listens for runner events and notifies HookExecutor
    about started iterations.
    """

    def __init__(self, key, task, subtask, workload, runner,
                 abort_on_sla_failure):
        """ResultConsumer constructor.

        :param key: Scenario identifier
        :param task: Instance of Task, task to run
        :param subtask: Instance of Subtask
        :param workload: Instance of Workload
        :param runner: ScenarioRunner instance that produces results to be
                       consumed
        :param abort_on_sla_failure: True if the execution should be stopped
                                     when some SLA check fails
        """

        self.key = key
        self.task = task
        self.subtask = subtask
        self.workload = workload
        self.runner = runner
        self.load_started_at = float("inf")
        self.load_finished_at = 0
        self.workload_data_count = 0

        self.sla_checker = sla.SLAChecker(key["kw"])
        self.hook_executor = hook.HookExecutor(key["kw"], self.task)
        self.abort_on_sla_failure = abort_on_sla_failure
        self.is_done = threading.Event()
        self.unexpected_failure = {}
        self.results = []
        self.thread = threading.Thread(target=self._consume_results)
        self.aborting_checker = threading.Thread(target=self.wait_and_abort)
        if "hooks" in self.key["kw"]:
            self.event_thread = threading.Thread(target=self._consume_events)

    def __enter__(self):
        self.thread.start()
        self.aborting_checker.start()
        if "hooks" in self.key["kw"]:
            self.event_thread.start()
        self.start = time.time()
        return self

    def _consume_results(self):
        task_aborted = False
        while True:
            if self.runner.result_queue:
                results = self.runner.result_queue.popleft()
                self.results.extend(results)
                for r in results:
                    self.load_started_at = min(r["timestamp"],
                                               self.load_started_at)
                    self.load_finished_at = max(r["duration"] + r["timestamp"],
                                                self.load_finished_at)
                    success = self.sla_checker.add_iteration(r)
                    if (self.abort_on_sla_failure and
                            not success and
                            not task_aborted):
                        self.sla_checker.set_aborted_on_sla()
                        self.runner.abort()
                        self.task.update_status(
                            consts.TaskStatus.SOFT_ABORTING)
                        task_aborted = True

                # save results chunks
                chunk_size = CONF.raw_result_chunk_size
                while len(self.results) >= chunk_size:
                    results_chunk = self.results[:chunk_size]
                    self.results = self.results[chunk_size:]
                    results_chunk.sort(key=lambda x: x["timestamp"])
                    self.workload.add_workload_data(self.workload_data_count,
                                                    {"raw": results_chunk})
                    self.workload_data_count += 1

            elif self.is_done.isSet():
                break
            else:
                time.sleep(0.1)

    def _consume_events(self):
        while not self.is_done.isSet() or self.runner.event_queue:
            if self.runner.event_queue:
                event = self.runner.event_queue.popleft()
                self.hook_executor.on_event(
                    event_type=event["type"], value=event["value"])
            else:
                time.sleep(0.01)

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self.finish = time.time()
        self.is_done.set()
        self.aborting_checker.join()
        self.thread.join()

        if exc_type:
            self.sla_checker.set_unexpected_failure(exc_value)

        if objects.Task.get_status(
                self.task["uuid"]) == consts.TaskStatus.ABORTED:
            self.sla_checker.set_aborted_manually()

        load_duration = max(self.load_finished_at - self.load_started_at, 0)

        LOG.info("Load duration is: %s" % utils.format_float_to_str(
            load_duration))
        LOG.info("Full runner duration is: %s" %
                 utils.format_float_to_str(self.runner.run_duration))
        LOG.info("Full duration is: %s" % utils.format_float_to_str(
            self.finish - self.start))

        results = {
            "load_duration": load_duration,
            "full_duration": self.finish - self.start,
            "sla": self.sla_checker.results(),
        }
        if "hooks" in self.key["kw"]:
            self.event_thread.join()
            results["hooks"] = self.hook_executor.results()

        if self.results:
            # NOTE(boris-42): Sort in order of starting
            #                 instead of order of ending
            self.results.sort(key=lambda x: x["timestamp"])
            self.workload.add_workload_data(self.workload_data_count,
                                            {"raw": self.results})

        self.workload.set_results(results)

    @staticmethod
    def is_task_in_aborting_status(task_uuid, check_soft=True):
        """Checks task is in abort stages

        :param task_uuid: UUID of task to check status
        :type task_uuid: str
        :param check_soft: check or not SOFT_ABORTING status
        :type check_soft: bool
        """
        stages = [consts.TaskStatus.ABORTING, consts.TaskStatus.ABORTED]
        if check_soft:
            stages.append(consts.TaskStatus.SOFT_ABORTING)
        return objects.Task.get_status(task_uuid) in stages

    def wait_and_abort(self):
        """Waits until abort signal is received and aborts runner in this case.

        Has to be run from different thread simultaneously with the
        runner.run method.
        """

        while not self.is_done.isSet():
            if self.is_task_in_aborting_status(self.task["uuid"],
                                               check_soft=False):
                self.runner.abort()
                self.task.update_status(consts.TaskStatus.ABORTED)
                break
            time.sleep(2.0)


class TaskAborted(Exception):
    """Task aborted exception

    Used by TaskEngine to interupt task run.
    """


class TaskEngine(object):
    """The Task engine class is used to execute benchmark scenarios.

    An instance of this class is initialized by the API with the task
    configuration and then is used to validate and execute all specified
    in config subtasks.

    .. note::

        Typical usage:
            ...

            engine = TaskEngine(config, task, deployment)
            engine.validate()   # to test config
            engine.run()        # to run config
    """

    def __init__(self, config, task, deployment,
                 abort_on_sla_failure=False):
        """TaskEngine constructor.

        :param config: Dict with configuration of specified benchmark scenarios
        :param task: Instance of Task,
                     the current task which is being performed
        :param deployment: Instance of Deployment,
        :param abort_on_sla_failure: True if the execution should be stopped
                                     when some SLA check fails
        """
        try:
            self.config = TaskConfig(config)
        except Exception as e:
            task.set_failed(type(e).__name__,
                            str(e),
                            json.dumps(traceback.format_exc()))
            if logging.is_debug():
                LOG.exception(e)
            raise exceptions.InvalidTaskException(str(e))

        self.task = task
        self.deployment = deployment
        self.abort_on_sla_failure = abort_on_sla_failure

    @logging.log_task_wrapper(LOG.info,
                              _("Task validation of scenarios names."))
    def _validate_config_scenarios_name(self, config):
        available = set(s.get_name() for s in scenario.Scenario.get_all())

        specified = set()
        for subtask in config.subtasks:
            for s in subtask.workloads:
                specified.add(s.name)

        if not specified.issubset(available):
            names = ", ".join(specified - available)
            raise exceptions.NotFoundScenarios(names=names)

    @logging.log_task_wrapper(LOG.info, _("Task validation of syntax."))
    def _validate_config_syntax(self, config):
        for subtask in config.subtasks:
            for workload in subtask.workloads:
                scenario_cls = scenario.Scenario.get(workload.name)
                namespace = scenario_cls.get_namespace()
                scenario_context = copy.deepcopy(
                    scenario_cls.get_default_context())

                results = []
                if workload.runner:
                    results.extend(runner.ScenarioRunner.validate(
                        name=workload.runner["type"],
                        credentials=None,
                        config=None,
                        plugin_cfg=workload.runner,
                        namespace=namespace))

                for context_name, context_conf in workload.context.items():
                    results.extend(context.Context.validate(
                        name=context_name,
                        credentials=None,
                        config=None,
                        plugin_cfg=context_conf,
                        namespace=namespace))

                for context_name, context_conf in scenario_context.items():
                    results.extend(context.Context.validate(
                        name=context_name,
                        credentials=None,
                        config=None,
                        plugin_cfg=context_conf,
                        namespace=namespace,
                        allow_hidden=True))

                for sla_name, sla_conf in workload.sla.items():
                    results.extend(sla.SLA.validate(
                        name=sla_name,
                        credentials=None,
                        config=None,
                        plugin_cfg=sla_conf))

                for hook_conf in workload.hooks:
                    results.extend(hook.Hook.validate(
                        name=hook_conf["name"],
                        credentials=None,
                        config=None,
                        plugin_cfg=hook_conf["args"]))

                    trigger_conf = hook_conf["trigger"]
                    results.extend(trigger.Trigger.validate(
                        name=trigger_conf["name"],
                        credentials=None,
                        config=None,
                        plugin_cfg=trigger_conf["args"]))

                if results:
                    msg = "\n ".join([str(r) for r in results])
                    kw = workload.make_exception_args(msg)
                    raise exceptions.InvalidTaskConfig(**kw)

    def _validate_config_semantic_helper(self, admin, user_context,
                                         workloads, platform):
        with user_context as ctx:
            ctx.setup()
            users = ctx.context["users"]
            for workload in workloads:
                results = scenario.Scenario.validate(
                    name=workload.name,
                    credentials={platform: {"admin": admin, "users": users}},
                    config=workload.to_dict(),
                    plugin_cfg=None)
                if results:
                    msg = "\n ".join([str(r) for r in results])
                    kw = workload.make_exception_args(msg)
                    raise exceptions.InvalidTaskConfig(**kw)

    @logging.log_task_wrapper(LOG.info, _("Task validation of semantic."))
    def _validate_config_semantic(self, config):
        # map workloads to platforms
        platforms = collections.defaultdict(list)
        for subtask in config.subtasks:
            for workload in subtask.workloads:
                # TODO(astudenov): We need to use a platform validator
                # in future to identify what kind of users workload
                # requires (regular users or admin)
                scenario_cls = scenario.Scenario.get(workload.name)
                namespace = scenario_cls.get_namespace()
                platforms[namespace].append(workload)

        for platform, workloads in platforms.items():
            creds = self.deployment.get_credentials_for(platform)

            admin = creds["admin"]
            if admin:
                admin.verify_connection()

            workloads_with_users = []
            workloads_with_existing_users = []

            for workload in workloads:
                if creds["users"] and "users" not in workload.context:
                    workloads_with_existing_users.append(workload)
                else:
                    workloads_with_users.append(workload)

            if workloads_with_users:
                ctx_conf = {"task": self.task,
                            "admin": {"credential": admin}}
                user_context = context.Context.get(
                    "users", namespace=platform,
                    allow_hidden=True)(ctx_conf)

                self._validate_config_semantic_helper(
                    admin, user_context, workloads_with_users, platform)

            if workloads_with_existing_users:
                ctx_conf = {"task": self.task,
                            "config": {"existing_users": creds["users"]}}
                # NOTE(astudenov): allow_hidden=True is required
                # for openstack existing_users context
                user_context = context.Context.get(
                    "existing_users", namespace=platform,
                    allow_hidden=True)(ctx_conf)

                self._validate_config_semantic_helper(
                    admin, user_context, workloads_with_existing_users,
                    platform)

    @logging.log_task_wrapper(LOG.info, _("Task validation."))
    def validate(self):
        """Perform full task configuration validation."""
        self.task.update_status(consts.TaskStatus.VALIDATING)
        try:
            self._validate_config_scenarios_name(self.config)
            self._validate_config_syntax(self.config)
            self._validate_config_semantic(self.config)
        except Exception as e:
            exception_info = json.dumps(traceback.format_exc(), indent=2,
                                        separators=(",", ": "))
            self.task.set_failed(type(e).__name__,
                                 str(e), exception_info)
            if logging.is_debug():
                LOG.exception(e)
            raise exceptions.InvalidTaskException(str(e))

    def _get_runner(self, config):
        config = config or {"type": "serial"}
        return runner.ScenarioRunner.get(config["type"])(self.task, config)

    def _prepare_context(self, ctx, name, owner_id):
        scenario_cls = scenario.Scenario.get(name)
        namespace = scenario_cls.get_namespace()

        creds = self.deployment.get_credentials_for(namespace)
        existing_users = creds["users"]

        scenario_context = copy.deepcopy(scenario_cls.get_default_context())
        if existing_users and "users" not in ctx:
            scenario_context.setdefault("existing_users", existing_users)
        elif "users" not in ctx:
            scenario_context.setdefault("users", {})

        scenario_context.update(ctx)
        context_obj = {
            "task": self.task,
            "owner_id": owner_id,
            "admin": {"credential": creds["admin"]},
            "scenario_name": name,
            "scenario_namespace": namespace,
            "config": scenario_context
        }

        return context_obj

    @logging.log_task_wrapper(LOG.info, _("Benchmarking."))
    def run(self):
        """Run the benchmark according to the test configuration.

        Test configuration is specified on engine initialization.

        :returns: List of dicts, each dict containing the results of all the
                  corresponding benchmark test launches
        """
        self.task.update_status(consts.TaskStatus.RUNNING)

        try:
            for subtask in self.config.subtasks:
                self._run_subtask(subtask)
        except TaskAborted:
            LOG.info("Received aborting signal.")
            self.task.update_status(consts.TaskStatus.ABORTED)
        else:
            if objects.Task.get_status(
                    self.task["uuid"]) != consts.TaskStatus.ABORTED:
                self.task.update_status(consts.TaskStatus.FINISHED)

    def _run_subtask(self, subtask):
        subtask_obj = self.task.add_subtask(**subtask.to_dict())

        try:
            # TODO(astudenov): add subtask context here
            for workload in subtask.workloads:
                self._run_workload(subtask_obj, workload)
        except TaskAborted:
            subtask_obj.update_status(consts.SubtaskStatus.ABORTED)
            raise
        except Exception as e:
            subtask_obj.update_status(consts.SubtaskStatus.CRASHED)
            # TODO(astudenov): save error to DB
            LOG.debug(traceback.format_exc())
            LOG.exception(e)

            # NOTE(astudenov): crash task after exception in subtask
            self.task.update_status(consts.TaskStatus.CRASHED)
            raise
        else:
            subtask_obj.update_status(consts.SubtaskStatus.FINISHED)

    def _run_workload(self, subtask_obj, workload):
        if ResultConsumer.is_task_in_aborting_status(self.task["uuid"]):
            raise TaskAborted()

        key = workload.make_key()
        workload_obj = subtask_obj.add_workload(key)
        LOG.info("Running benchmark with key: \n%s"
                 % json.dumps(key, indent=2))
        runner_obj = self._get_runner(workload.runner)
        context_obj = self._prepare_context(
            workload.context, workload.name, workload_obj["uuid"])
        try:
            with ResultConsumer(key, self.task, subtask_obj, workload_obj,
                                runner_obj, self.abort_on_sla_failure):
                with context.ContextManager(context_obj):
                    runner_obj.run(workload.name, context_obj,
                                   workload.args)
        except Exception as e:
            LOG.debug(traceback.format_exc())
            LOG.exception(e)
            # TODO(astudenov): save error to DB


class TaskConfig(object):
    """Version-aware wrapper around task.

    """

    HOOK_CONFIG = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "args": {},
            "trigger": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "args": {},
                },
                "required": ["name", "args"],
                "additionalProperties": False,
            }
        },
        "required": ["name", "args", "trigger"],
        "additionalProperties": False,
    }

    CONFIG_SCHEMA_V1 = {
        "type": "object",
        "$schema": consts.JSON_SCHEMA,
        "patternProperties": {
            ".*": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "args": {"type": "object"},
                        "description": {
                            "type": "string"
                        },
                        "runner": {
                            "type": "object",
                            "properties": {"type": {"type": "string"}},
                            "required": ["type"]
                        },
                        "context": {"type": "object"},
                        "sla": {"type": "object"},
                        "hooks": {
                            "type": "array",
                            "items": HOOK_CONFIG,
                        }
                    },
                    "additionalProperties": False
                }
            }
        }
    }

    CONFIG_SCHEMA_V2 = {
        "type": "object",
        "$schema": consts.JSON_SCHEMA,
        "properties": {
            "version": {"type": "number"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "tags": {
                "type": "array",
                "items": {"type": "string"}
            },

            "subtasks": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "group": {"type": "string"},
                        "description": {"type": "string"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"}
                        },

                        "run_in_parallel": {"type": "boolean"},
                        "workloads": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "description": {"type": "string"},
                                    "args": {"type": "object"},

                                    "runner": {
                                        "type": "object",
                                        "properties": {
                                            "type": {"type": "string"}
                                        },
                                        "required": ["type"]
                                    },

                                    "sla": {"type": "object"},
                                    "hooks": {
                                        "type": "array",
                                        "items": HOOK_CONFIG,
                                    },
                                    "context": {"type": "object"}
                                },
                                "additionalProperties": False,
                                "required": ["name", "runner"]
                            }
                        }
                    },
                    "additionalProperties": False,
                    "required": ["title", "workloads"]
                }
            }
        },
        "additionalProperties": False,
        "required": ["title", "subtasks"]
    }

    CONFIG_SCHEMAS = {1: CONFIG_SCHEMA_V1, 2: CONFIG_SCHEMA_V2}

    def __init__(self, config):
        """TaskConfig constructor.

        :param config: Dict with configuration of specified task
        """
        if config is None:
            # NOTE(stpierre): This gets reraised as
            # InvalidTaskException. if we raise it here as
            # InvalidTaskException, then "Task config is invalid: "
            # gets prepended to the message twice.
            raise Exception(_("Input task is empty"))

        self.version = self._get_version(config)
        self._validate_version()
        self._validate_json(config)

        self.title = config.get("title", "Task")
        self.tags = config.get("tags", [])
        self.description = config.get("description")

        self.subtasks = self._make_subtasks(config)

        # if self.version == 1:
        # TODO(ikhudoshyn): Warn user about deprecated format

    @staticmethod
    def _get_version(config):
        return config.get("version", 1)

    def _validate_version(self):
        if self.version not in self.CONFIG_SCHEMAS:
            allowed = ", ".join([str(k) for k in self.CONFIG_SCHEMAS])
            msg = (_("Task configuration version {0} is not supported. "
                     "Supported versions: {1}")).format(self.version, allowed)
            raise exceptions.InvalidTaskException(msg)

    def _validate_json(self, config):
        try:
            jsonschema.validate(config, self.CONFIG_SCHEMAS[self.version])
        except Exception as e:
            raise exceptions.InvalidTaskException(str(e))

    def _make_subtasks(self, config):
        if self.version == 2:
            return [SubTask(s) for s in config["subtasks"]]
        elif self.version == 1:
            subtasks = []
            for name, v1_workloads in config.items():
                for v1_workload in v1_workloads:
                    v2_workload = copy.deepcopy(v1_workload)
                    v2_workload["name"] = name
                    subtasks.append(
                        SubTask({"title": name, "workloads": [v2_workload]}))
            return subtasks


class SubTask(object):
    """Subtask -- unit of execution in Task

    """
    def __init__(self, config):
        """Subtask constructor.

        :param config: Dict with configuration of specified subtask
        """
        self.title = config["title"]
        self.tags = config.get("tags", [])
        self.group = config.get("group")
        self.description = config.get("description")
        self.workloads = [Workload(wconf, pos)
                          for pos, wconf in enumerate(config["workloads"])]
        self.context = config.get("context", {})

    def to_dict(self):
        return {
            "title": self.title,
            "description": self.description,
            "context": self.context,
        }


class Workload(object):
    """Workload -- workload configuration in SubTask.

    """
    def __init__(self, config, pos):
        self.name = config["name"]
        self.description = config.get("description", "")
        if not self.description:
            try:
                self.description = scenario.Scenario.get(
                    self.name).get_info()["title"]
            except (exceptions.PluginNotFound,
                    exceptions.MultipleMatchesFound):
                # let's fail an issue with loading plugin at a validation step
                pass
        self.runner = config.get("runner", {})
        self.sla = config.get("sla", {})
        self.hooks = config.get("hooks", [])
        self.context = config.get("context", {})
        self.args = config.get("args", {})
        self.pos = pos

    def to_dict(self):
        workload = {"runner": self.runner}

        for prop in "sla", "args", "context", "hooks":
            value = getattr(self, prop)
            if value:
                workload[prop] = value

        return workload

    def to_task(self):
        """Make task configuration for the workload.

        This method returns a dict representing full configuration
        of the task containing a single subtask with this single
        workload.

        :return: dict containing full task configuration
        """
        # NOTE(ikhudoshyn): Result of this method will be used
        # to store full task configuration in DB so that
        # subtask configuration in reports would be given
        # in the same format as it was provided by user.
        # Temporarily it returns to_dict() in order not
        # to break existing reports. It should be
        # properly implemented in a patch that will update reports.
        # return {self.name: [self.to_dict()]}
        return self.to_dict()

    def make_key(self):
        return {"name": self.name,
                "description": self.description,
                "pos": self.pos,
                "kw": self.to_task()}

    def make_exception_args(self, reason):
        return {"name": self.name,
                "pos": self.pos,
                "config": self.to_dict(),
                "reason": reason}
