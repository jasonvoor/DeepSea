# -*- coding: utf-8 -*-
"""
This module is responsible for the parsing of DeepSea stage files
"""
from __future__ import absolute_import
from __future__ import print_function

import glob
import logging
import os
import pickle

import salt.client

from .common import redirect_stdout


# pylint: disable=C0103
logger = logging.getLogger(__name__)


__opts__ = salt.config.minion_config('/etc/salt/minion')
__opts__['file_client'] = 'local'
__caller__ = salt.client.Caller(mopts=__opts__)


class OrchestrationNotFound(Exception):
    """
    No orchestration file found exception
    """
    pass


class SLSRenderer(object):
    """
    Helper class to render sls files
    """

    @staticmethod
    def render(file_name):
        """
        This function makes use of slsutil salt module to render sls files
        Args:
            file_name (str): the sls file path
        """
        with redirect_stdout(os.devnull):
            result = __caller__.cmd('slsutil.renderer', file_name)
        return result


class SLSParser(object):
    """
    SLS files parser
    """

    _CACHE_FILE_PREFIX_ = "_deepsea"
    _CACHE_DIR_PATH_ = "/tmp"

    @staticmethod
    def _state_name_is_dir(state_name):
        """
        Checks wheather a state_name corresponds to a directory in the filesystem.
        """
        path = "/srv/salt/{}".format(state_name.replace(".", "/"))
        return os.path.isdir(path)

    @staticmethod
    def _state_file_path(state_name):
        """
        Returns the filesystem path of a state file
        Args:
            state_name (str): the salt state name
        """
        if SLSParser._state_name_is_dir(state_name):
            path = "/srv/salt/{}/init.sls".format(state_name.replace(".", "/"))
        else:
            path = "/srv/salt/{}.sls".format(state_name.replace(".", "/"))

        if not os.path.exists(path):
            raise OrchestrationNotFound("could not determine path for {}"
                                        .format(state_name))

        return path

    @staticmethod
    def _gen_state_name_from_include(parent_state, include):
        """
        Generates the salt state name from a state include path.
        Example:
        ceph.stage.4 state contents:

        .. code-block:: yaml
            include:
              - ..iscsi

        The state name generated by this include will be:
        ceph.stage.iscsi
        """
        # counting dots
        dot_count = 0
        for c in include:
            if c == '.':
                dot_count += 1
            else:
                break
        include = include[dot_count:]
        if not SLSParser._state_name_is_dir(parent_state):
            # we need to remove the "file_name" part of the parent state name
            dot_count += 1
        if dot_count > 1:
            # The state it's not ceph.stage.4.iscsi but ceph.stage.iscsi if
            # the include has two dots (..) in it.
            parent_state = ".".join(parent_state.split('.')[:-(dot_count - 1)])

        return "{}.{}".format(parent_state, include)

    @staticmethod
    def _traverse_state(state_name, stages_only, cache):
        """
        Parses the all steps (actions) triggered by the execution of a state file.
        It recursevely follows "include" directives, and state files.
        Args:
            state_name (str): the salt state name, e.g., ceph.stage.1
            only_events (bool): wheather to parse state declarations that have fire_event=True
            cache (bool): wheather load/store the results in a cache file

        Returns:
            list(StepType): a list of steps
        """
        if stages_only and not state_name.startswith('ceph.stage'):
            return []

        cache_file_path = '{}/{}_{}_{}.bin'.format(SLSParser._CACHE_DIR_PATH_,
                                                   SLSParser._CACHE_FILE_PREFIX_,
                                                   stages_only, state_name)
        if cache:
            if os.path.exists(cache_file_path):
                logger.info("state %s found in cache, loading from cache...", state_name)
                # pylint: disable=W8470
                with open(cache_file_path, mode='rb') as binfile:
                    return pickle.load(binfile)

        result = []
        path = SLSParser._state_file_path(state_name)
        state_dict = SLSRenderer.render(path)
        logger.info("Parsing state file: %s", path)
        for key, steps in state_dict.items():
            if key == 'include':
                for inc in state_dict['include']:
                    logger.debug("Handling include of: parent={} include={}"
                                 .format(state_name, inc))
                    include_state_name = SLSParser._gen_state_name_from_include(state_name, inc)
                    result.extend(SLSParser._traverse_state(include_state_name, stages_only,
                                                            cache))
            else:
                if isinstance(steps, dict):
                    for fun, args in steps.items():
                        logger.debug("Parsing step: desc={} fun={} step={}".format(key, fun, args))
                        if fun == 'salt.state':
                            state = SaltState(key, args)
                            result.append(state)
                            result.extend(SLSParser._traverse_state(state.state, stages_only,
                                                                    cache))
                        elif fun == 'salt.runner':
                            result.append(SaltRunner(key, args))
                        elif fun == 'module.run':
                            result.append(SaltModule(key, args))
                        else:
                            result.append(SaltBuiltIn(key, fun, args))

        if cache:
            # pylint: disable=W8470
            with open(cache_file_path, mode='wb') as binfile:
                pickle.dump(result, binfile)

        return result

    @staticmethod
    def _search_step(steps, mod_name, sid):
        """
        Searches a step that matches the module name and state id
        Args:
            steps (list): list of steps
            mod_name (str): salt module name, can be None
            sid (str): state id
        """
        for step in steps:
            if mod_name:
                if isinstance(step, SaltRunner):
                    if mod_name != 'salt':
                        continue
                elif isinstance(step, SaltState):
                    if mod_name != 'salt':
                        continue
                else:
                    step_mod = step.fun[:step.fun.find('.')]
                    if mod_name != step_mod:
                        continue
            name_arg = step.get_arg('name')
            if step.desc == sid or (name_arg and name_arg == sid):
                return step
        return None

    @staticmethod
    def parse_state_steps(state_name, stages_only=True, cache=True):
        """
        Parses the all steps (actions) triggered by the execution of a state file
        Args:
            state_name (str): the salt state name, e.g., ceph.stage.1
            only_events (bool): wheather to parse state declarations that have fire_event=True
            cache (bool): wheather load/store the results in a cache file

        Returns:
            list(StepType): a list of steps
        """
        result = SLSParser._traverse_state(state_name, stages_only, cache)

        def process_requisite_directive(step, directive):
            """
            Processes a requisite directive
            """
            req = step.get_arg(directive)
            if req:
                if not isinstance(req, list):
                    # usually req will be a list of dicts, this is just for
                    # the case when req is not a list and maintain the same code
                    # below
                    req = [req]

                for req in req:
                    if isinstance(req, dict):
                        for mod, sid in req.items():
                            req_step = SLSParser._search_step(result, mod, sid)
                            assert req_step
                            if directive in ['require', 'watch', 'onchanges']:
                                step.on_success_deps.append(req_step)
                            elif directive == 'onfail':
                                step.on_fail_deps.append(req_step)
                    else:
                        req_step = SLSParser._search_step(result, None, req)
                        assert req_step
                        if directive in ['require', 'watch', 'onchanges']:
                            step.on_success_deps.append(req_step)
                        elif directive == 'onfail':
                            step.on_fail_deps.append(req_step)

        # process state requisites
        for step in result:
            for directive in ['require', 'watch', 'onchanges', 'onfail']:
                process_requisite_directive(step, directive)

        return result

    @staticmethod
    def clean_cache(state_name):
        """
        Deletes all cache files
        """
        if not state_name:
            cache_files = '{}/{}_*.bin'.format(SLSParser._CACHE_DIR_PATH_,
                                               SLSParser._CACHE_FILE_PREFIX_)
        else:
            cache_files = '{}/{}_*_{}.bin'.format(SLSParser._CACHE_DIR_PATH_,
                                                  SLSParser._CACHE_FILE_PREFIX_,
                                                  state_name)
        logger.info("cleaning cache: %s", cache_files)
        for cache_file in glob.glob(cache_files):
            os.remove(cache_file)


class SaltStep(object):
    """
    Base class to represent a single stage step
    """
    def __init__(self, desc, args):
        self.desc = desc
        self.args = args
        self.on_success_deps = []
        self.on_fail_deps = []

    def __str__(self):
        return self.desc

    def get_arg(self, key):
        """
        Returns the arg value for the key
        """
        if isinstance(self.args, dict):
            if key in self.args:
                return self.args[key]
        elif isinstance(self.args, list):
            arg = [arg for arg in self.args if key in arg]
            if arg:
                return arg[0][key]
        else:
            assert False
        return None


class SaltState(SaltStep):
    """
    Class to represent a Salt state apply step
    """
    def __init__(self, desc, args):
        super(SaltState, self).__init__(desc, args)
        self.state = self.get_arg('sls')
        self.target = self.get_arg('tgt')

    def __str__(self):
        return "SaltState(desc: {}, state: {}, target: {})".format(self.desc, self.state,
                                                                   self.target)


class SaltRunner(SaltStep):
    """
    Class to represent a Salt runner step
    """
    def __init__(self, desc, args):
        super(SaltRunner, self).__init__(desc, args)
        self.fun = self.get_arg('name')

    def __str__(self):
        return "SaltRunner(desc: {}, fun: {})".format(self.desc, self.fun)


class SaltModule(SaltStep):
    """
    Class to represent a Salt module step
    """
    def __init__(self, desc, args):
        super(SaltModule, self).__init__(desc, args)
        self.fun = self.get_arg('name')

    def __str__(self):
        return "SaltModule(desc: {}, fun: {})".format(self.desc, self.fun)


class SaltBuiltIn(SaltStep):
    """
    Class to represent a Salt built-in command step

    Built-in commands like cmd.run and file.managed need
    to be condensed.
    """
    def __init__(self, desc, fun, args):
        super(SaltBuiltIn, self).__init__(desc, args)
        self.fun = fun
        self.args = dict()
        for arg in args:
            if isinstance(arg, dict):
                for key, val in arg.items():
                    self.args[key] = val
            else:
                self.args['nokey'] = arg

    def __str__(self):
        return "SaltBuiltIn(desc: {}, fun: {}, args: {})".format(self.desc, self.fun, self.args)