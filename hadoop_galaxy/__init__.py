# BEGIN_COPYRIGHT
#
# Copyright (C) 2014 CRS4.
#
# This file is part of hadoop-galaxy, released under the terms of the BSD
# 3-Clause License <http://opensource.org/licenses/BSD-3-Clause>.
#
# END_COPYRIGHT

import argparse
import copy
import logging
import os
import subprocess
import sys
import yaml

import pydoop.hdfs as phdfs

from hadoop_galaxy.pathset import Pathset, FilePathset
from hadoop_galaxy.utils import get_abs_executable_path

EnvOutputDataDir = 'HADOOP_GALAXY_DATA_DIR'
EnvConfPath = 'HADOOP_GALAXY_CONF'

log = logging.getLogger('HadoopGalaxy')


# The configuration file:
#
# We have a partly implemented feature for specifying some configuration parameters
# through a configuration file, rather than through environment variables or
# command-line arguments.  I started writing some documentation to put in the README,
# but then decided not to insert it because the feature is not really complete since
# the use of the configuration file isn't coherent across all Hadoop-Galaxy components.
#
# I'm dumping the partly written documentation here, for future use.
##########################################################################################
#  **HADOOP\_GALAXY\_CONF**: path to configuration file where you can specify the
#  same options as the preceding environment variables, and more. This variable is
#  not yet respected by all Hadoop-Galaxy components (work-in-progress).
#
#
#  #### Configuration file
#
#  Some Hadoop-Galaxy components, including the adapter, support reading a yaml
#  configuration file where you can configure the behaviour of the component and,
#  for the adapter, configure how the Hadoop-based tool is run (e.g., set
#  environment variables).
#
#  The following keys are recognized:
#
#  * HADOOP_HOME
#  * HADOOP_CONF_DIR
#  * tool\_env: next key-value paires to set environment
##########################################################################################



class HadoopToolRunner(object):
  """
  Implements the logic necessary to run a Hadoop-based tool from
  within Galaxy.

  There are two reasons why this class is necessary.

  The first is that typical Hadoop programs produce an output directory
  containing many files.  Galaxy, on the other hand, better supports the case
  where a tool reads one input file and produces an output file.  It also
  supports multiple output files, but it seems to insist on trying to open the
  main output path as a file (which causes a problem since Hadoop produces a
  directory).

  The second issue is that, since Hadoop programs usually process large data sets
  and often operate on HDFS, one may not want to store those data sets in the Galaxy
  'file_path' directory (its configured data set location).

  To address these issues, we create a level of indirection.  The HadoopToolRunner reads
  as input a FilePathset and produces a FilePathset.  These are the job data sets, as far
  as Galaxy is concerned.  These Pathsets contain URIs to the real data files.  In turn,
  HadoopToolRunner invokes the Hadoop-based program providing it with the contents of the
  input Pathset as input paths, and recording its output directory in an output FilePathset
  (the output data set provided to Galaxy).

  The HadoopToolRunner also sets up the necessary Hadoop environment and forwards unrecognized
  arguments down to the actual tool executable.
  """

  def __init__(self, executable):
    """
    Initialize a HadoopToolRunner for a specific tool.

    tool_name will be used as the executable name.
    """
    self.executable = executable
    # a list of input paths, potentially containing wildcards that will be expanded by hadoop
    self.input_params = None
    # string -- output path
    self.output_str = None
    # a list of options
    self.generic_opts = []

  def __str__(self):
    return '\n\t'.join(
       (str(type(self)),
        "executable: %s" % self.executable,
        "input: %s"  % str(self.input_params),
        "output: %s" % self.output_str,
        "opts: %s"   % str(self.generic_opts))
      )

  def set_input(self, pset):
    """
    Set the input paths for the Hadoop command.
    """
    self.input_params = pset.get_paths()

  def set_output(self, pset):
    """
    Set the output path for the Hadoop command.
    """
    if len(pset) != 1:
      raise RuntimeError("Expecting an output pathset containing one path, but got %d" % len(pset))
    self.output_str = iter(pset).next()

  def parse_args(self, args_list):
    """
    Gets the remainder of the arguments, split by argparse and sets them to
    self.generic_opts.  The arguments will be passed to the Hadoop tool as
    generic options (placed them between the command name and the input path.

    This method can be overridden to implement more sophisticated parsing
    strategies.
    """
    self.generic_opts = args_list

  def command(self, env=None):
    """
    Returns the arguments array to run this Hadoop command.

    The returned array can be passed to subprocess.call and friends.

    If the executable doesn't specify an absolute path, this method looks for the
    program in the paths listed by the PATH environment variable.  It also
    verifies that the input and output parameters have been set.
    """
    if self.executable is None:
      raise RuntimeError("executable not set!")
    if self.input_params is None:
      raise RuntimeError("Input parameters not set!")
    if self.output_str is None:
      raise RuntimeError("output path not set!")

    full_path = get_abs_executable_path(self.executable, (env or os.environ))
    logging.getLogger(self.__class__.__name__).debug("Found tool: %s", full_path)
    return [full_path] + self.generic_opts + self.input_params + [self.output_str]

  def execute(self, logger, env=None):
    """
    Executes the command.

    This method calls self.command to build the command array and then executes
    the command.  If provided, the specified `env` will be used.
    """
    cmd = self.command(env)
    logger.debug("attempting to remove output path %s", self.output_str)
    try:
      phdfs.rmr(self.output_str)
    except IOError as e:
      logger.warning(e)

    if not phdfs.path.exists(phdfs.path.dirname(self.output_str)):
      phdfs.mkdir(phdfs.path.dirname(self.output_str))
      logger.debug("Created parent of output directory")

    logger.info("Executing command: %s", cmd)
    logger.debug("PATH: %s", (env or os.environ).get('PATH'))
    subprocess.check_call(cmd, env=env)


class HadoopGalaxy(object):
    HadoopOutputDirName = 'hadoop_output'

    @staticmethod
    def build_parser():
        """
        Build an arg parser for our "standard" command line.  The
        parser is returned so that the client can optionally add
        to it or modify it.
        """
        parser = argparse.ArgumentParser(description="Wrap Hadoop-based tools to run within Galaxy")
        parser.add_argument('--input', metavar="InputPath", required=True,
                help="Path to input pathset provided by Galaxy.")
        parser.add_argument('--input-format', metavar="InputFormat", help="Input format provided by Galaxy.")
        parser.add_argument('--output', metavar="OutputPath", required=True,
                help="Output path provided by Galaxy")
        parser.add_argument('--append-python-path', metavar="PATH",
                 help="Path to append to the PYTHONPATH before calling the executable")
        parser.add_argument('--output-data-dir', metavar="PATH",
                help="URI to a working directory where the Hadoop job will write its output. Can also be " +\
                        "set through HADOOP_GALAXY_DATA_DIR env. variable (default: Galaxy data dir).")
        parser.add_argument('--conf', metavar="conf_file", help="Hadoop+Galaxy configuration file")
        parser.add_argument('remaining_args', nargs=argparse.REMAINDER)
        return parser

    @staticmethod
    def parse_args(parser, args=None):
        """
        Simple helper method to avoid having to re-type boilerplate code
        """
        if args is None:
            args = sys.argv[1:] # skip the program name
        options = parser.parse_args(args)
        return options

    def __init__(self):
        self.conf = dict()
        self._runner = None
        self._cmd_env = dict()

    @property
    def runner(self):
        return self._runner

    @runner.setter
    def runner(self, r):
        self._runner = r

    def _set_hadoop_conf(self):
        """
        If our configuration contains HADOOP_HOME or HADOOP_CONF_DIR
        copy them to our environment.  Else, whatever is in the
        current environment will remain as such.
        """
        if self.conf.has_key('HADOOP_HOME'):
            self._cmd_env['HADOOP_HOME'] = self.conf['HADOOP_HOME']
        if self.conf.has_key('HADOOP_CONF_DIR'):
            self._cmd_env['HADOOP_CONF_DIR'] = self.conf['HADOOP_CONF_DIR']

    def gen_data_output_path(self, options, name=None):
        """
        Generate an output path for the data produced by the hadoop job.

        The default behaviour is to use the path provided for the output pathset
        (options.output) as a base.  Therefore, the data path is created as

            os.path.dirname(options.output)/hadoop_output/os.path.basename(options.output)

        So, in a typicaly situation a directory "hadoop_output" will be created
        in the Galaxy data directory and the job output dir will be created
        inside it (with the same name as the galaxy dataset).

        .. note:  in this manner your Hadoop job will not write to HDFS; instead,
            it will write to the same storage as Galaxy.

        This set-up is not ideal for most applications.  So, this default directory
        for hadoop output can be overridden:

          * through options.output_data_dir, with first precendence;
          * through the environment variable HADOOP_GALAXY_DATA_DIR.

        In these cases the Hadoop job output will be sent to

            options.output_data_dir/os.path.basename(options.output)

        The name of the last component of the path (os.path.basename(...)) can be
        explicitly set by passing a value for the `name` function argument.
        """
        if name:
            suffix_path = name
        else:
            # We'll use the name of the output file as the name of the data file,
            # knowing that the datapath (below) will be calculated as to not put data
            # and pathset file in the same place.
            suffix_path = os.path.basename(options.output)

        if options.output_data_dir:
            datapath = options.output_data_dir
        elif os.environ.get(EnvOutputDataDir):
            datapath = os.environ.get(EnvOutputDataDir)
        else:
            datapath = os.path.join(os.path.dirname(options.output), self.HadoopOutputDirName)

        p = os.path.join(datapath, suffix_path)
        log.info("Hadoop job data output path %s", p)
        return p

    def _configure_for_job(self, options):
        self._cmd_env = copy.copy(os.environ)
        conf_file = options.conf or os.environ.get(EnvConfPath)
        if conf_file:
            log.debug("loading config from %s", conf_file)
            try:
                with open(conf_file) as f:
                    self.conf = yaml.load(f)
                log.debug("loaded conf: %s", self.conf)
            except IOError as e:
              log.critical("Couldn't read the specified configuration from %s", conf_file)
              log.exception(e)
              sys.exit(1)
            except yaml.YAMLError as e:
              log.critical("Error parsing configuration file %s", conf_file)
              log.exception(e)
              raise
        else:
            self.conf = dict()

        self._set_hadoop_conf()

        if options.remaining_args:
            self._runner.parse_args(options.remaining_args)

        # If the configuration specifies a dict for 'tool_env' use it to override
        # environment variables
        tool_env = self.conf.get('tool_env')
        if tool_env:
            log.debug("Overriding environment variables from configuration")
            for k, v in tool_env.iteritems():
                log.debug("env[%s] = %s", k, v)
                self._cmd_env[k] = v

        if log.isEnabledFor(logging.INFO):
            log.info("Hadoop settings:")
            for k, v in self._cmd_env.iteritems():
                if k.startswith("HADOOP"):
                    log.info("%s = %s", k, v)

    def run(self, options):
        log.debug("options: %s", options)
        self._configure_for_job(options)

        # load input pathset
        with open(options.input) as f:
            input_pathset = FilePathset.from_file(f)
            log.debug("Read input pathset: %s", input_pathset)

        # new pathset with a single output path
        output_pathset = FilePathset(self.gen_data_output_path(options))

        try:
            self._runner.set_input(input_pathset)
            self._runner.set_output(output_pathset)
            log.debug("Executing: %s", self._runner)
            self._runner.execute(log, self._cmd_env)
            with open(options.output, 'w') as f:
                output_pathset.write(f)
        except subprocess.CalledProcessError as e:
            log.exception(e)
            if e.returncode < 0:
                msg = "%s was terminated by signal %d" % (options.executable, e.returncode)
            elif e.returncode > 0:
                msg = "%s exit code: %d" % (options.executable, e.returncode)
            log.critical(msg)
            raise RuntimeError(msg)
        except OSError as e:
            log.critical("Command execution failed")
            log.exception(e)
            raise e

def main(args=None):
    hg = HadoopGalaxy()
    parser = hg.build_parser()
    parser.add_argument('--executable', metavar="Program", help="The Hadoop program to run")
    options = hg.parse_args(parser, args)
    if not options.executable:
        raise ValueError("You need to specify the program to run with the --executable option")
    hg.runner = HadoopToolRunner(options.executable)
    hg.run(options)
