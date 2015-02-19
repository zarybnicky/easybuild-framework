##
# Copyright 2012-2014 Ghent University
#
# This file is part of EasyBuild,
# originally created by the HPC team of Ghent University (http://ugent.be/hpc/en),
# with support of Ghent University (http://ugent.be/hpc),
# the Flemish Supercomputer Centre (VSC) (https://vscentrum.be/nl/en),
# the Hercules foundation (http://www.herculesstichting.be/in_English)
# and the Department of Economy, Science and Innovation (EWI) (http://www.ewi-vlaanderen.be/en).
#
# http://github.com/hpcugent/easybuild
#
# EasyBuild is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation v2.
#
# EasyBuild is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with EasyBuild.  If not, see <http://www.gnu.org/licenses/>.
##
"""
Interface module to TORQUE (PBS).

@author: Stijn De Weirdt (Ghent University)
@author: Toon Willems (Ghent University)
@author: Kenneth Hoste (Ghent University)
"""

import os
import tempfile
import time
from vsc.utils import fancylogger

from easybuild.tools.job import JobServer


_log = fancylogger.getLogger('pbs_python', fname=False)


MAX_WALLTIME = 72
# extend paramater should be 'NULL' in some functions because this is required by the python api
NULL = 'NULL'
# list of known hold types
KNOWN_HOLD_TYPES = []

try:
    from PBSQuery import PBSQuery
    import pbs
    KNOWN_HOLD_TYPES = [pbs.USER_HOLD, pbs.OTHER_HOLD, pbs.SYSTEM_HOLD]
    # `pbs_python` available, no need guard against import errors
    def only_if_pbs_import_successful(fn):
        return fn
    HAVE_PBS_PYTHON = True
except ImportError:
    _log.debug("Failed to import pbs from pbs_python."
               " Silently ignoring, this is a real issue only with --job=pbs")
    # no `pbs_python` available, turn function into a no-op
    def only_if_pbs_import_successful(fn):
        def instead(*args, **kwargs):
            """This is a no-op since `pbs_python` is not available."""
            errmsg = ("PBSQuery or pbs modules not available."
                      " Please make sure `pbs_python` is installed and usable.")
            _log.error(errmsg)
            raise RuntimeError(errmsg)
        return instead
    HAVE_PBS_PYTHON = False


class Pbs(JobServer):
    """
    Manage PBS server communication and create `PbsJob` objects.
    """

    USABLE = HAVE_PBS_PYTHON

    def __init__(self, pbs_server=None):
        self.pbs_server = pbs_server or pbs.pbs_default()
        self.conn = None
        self._ppn = None

    def begin(self):
        self.connect_to_server()
        self._submitted = []

    @only_if_pbs_import_successful
    def connect_to_server(self):
        """Connect to PBS server, set and return connection."""
        if not self.conn:
            self.conn = pbs.pbs_connect(self.pbs_server)
        return self.conn

    def submit(self, job, after=frozenset()):
        assert isinstance(job, PbsJob)
        if after:
            job.add_dependencies(after)
        job._submit()
        self._submitted.append(job)

    def commit(self):
        # release all user holds on jobs after submission is completed
        for job in self._submitted:
            if job.has_holds():
                _log.info("releasing user hold on job %s" % job.jobid)
                job.release_hold()
        self.disconnect_from_server()
        if self._submitted:
            _log.info("List of submitted jobs:")
            for job in self._submitted:
                _log.info("* %s (%s): %s", job.name, job.module, job.jobid)

    @only_if_pbs_import_successful
    def disconnect_from_server(self):
        """Disconnect current connection."""
        pbs.pbs_disconnect(self.conn)
        self.conn = None

    def _get_ppn(self):
        """Guess PBS' `ppn` value for a full node."""
        # cache this value as it's not likely going to change over the
        # `eb` script runtime ...
        if not self._ppn:
            pq = PBSQuery()
            node_vals = pq.getnodes().values()  # only the values, not the names
            interesting_nodes = ('free', 'job-exclusive',)
            res = {}
            for np in [int(x['np'][0]) for x in node_vals if x['state'][0] in interesting_nodes]:
                res.setdefault(np, 0)
                res[np] += 1

            # return most frequent
            freq_count, freq_np = max([(j, i) for i, j in res.items()])
            _log.debug("Found most frequent np %s (%s times) in interesting nodes %s" % (freq_np, freq_count, interesting_nodes))

            self._ppn = freq_np

        return self._ppn

    ppn = property(_get_ppn)

    def make_job(self, script, name, env_vars=None, hours=None, cores=None):
        """Create and return a `PbsJob` object with the given parameters."""
        return PbsJob(self, script, name, env_vars, hours, cores,
                      conn=self.conn, ppn=self.ppn)


class PbsJob(object):
    """Interaction with TORQUE"""

    def __init__(self, server, script, name, env_vars=None,
                 hours=None, cores=None, conn=None, ppn=None):
        """
        create a new Job to be submitted to PBS
        env_vars is a dictionary with key-value pairs of environment variables that should be passed on to the job
        hours and cores should be integer values.
        hours can be 1 - MAX_WALLTIME, cores depends on which cluster it is being run.
        """
        self.log = fancylogger.getLogger(self.__class__.__name__, fname=False)

        self._server = server
        self.script = script
        if env_vars:
            self.env_vars = env_vars.copy()
        else:
            self.env_vars = {}
        self.name = name

        try:
            self.pbsconn = self._server.connect_to_server()
        except Exception, err:
            self.log.error("Failed to connect to PBS server: %s" % err)
            raise

        # setup the resources requested

        # validate requested resources!
        if hours is None:
            hours = MAX_WALLTIME
        if hours > MAX_WALLTIME:
            self.log.warn("Specified %s hours, but this is impossible. (resetting to %s hours)" % (hours, MAX_WALLTIME))
            hours = MAX_WALLTIME

        if ppn is None:
            max_cores = server.ppn
        else:
            max_cores = ppn
        if cores is None:
            cores = max_cores
        if cores > max_cores:
            self.log.warn("number of requested cores (%s) was greater than available (%s) " % (cores, max_cores))
            cores = max_cores

        # only allow cores and hours for now.
        self.resources = {
                          "walltime": "%s:00:00" % hours,
                          "nodes": "1:ppn=%s" % cores
                         }
        # don't specify any queue name to submit to, use the default
        self.queue = None
        # job id of this job
        self.jobid = None
        # list of dependencies for this job
        self.deps = []
        # list of holds that are placed on this job
        self.holds = []

    def __str__(self):
        """Return the job ID as a string."""
        return (str(self.jobid) if self.jobid is not None
                else repr(self))

    def add_dependencies(self, jobs):
        """
        Add dependencies to this job.

        Argument `jobs` is a sequence of `PbsJob` objects.
        """
        self.deps.extend(jobs)

    def _submit(self):
        """Submit the jobscript txt, set self.jobid"""
        txt = self.script
        self.log.debug("Going to submit script %s" % txt)

        # Build default pbs_attributes list
        pbs_attributes = pbs.new_attropl(1)
        pbs_attributes[0].name = pbs.ATTR_N  # Job_Name
        pbs_attributes[0].value = self.name

        # set resource requirements
        resource_attributes = pbs.new_attropl(len(self.resources))
        idx = 0
        for k, v in self.resources.items():
            resource_attributes[idx].name = pbs.ATTR_l  # Resource_List
            resource_attributes[idx].resource = k
            resource_attributes[idx].value = v
            idx += 1
        pbs_attributes.extend(resource_attributes)

        # add job dependencies to attributes
        if self.deps:
            deps_attributes = pbs.new_attropl(1)
            deps_attributes[0].name = pbs.ATTR_depend
            deps_attributes[0].value = ",".join(["afterany:%s" % dep.jobid for dep in self.deps])
            pbs_attributes.extend(deps_attributes)
            self.log.debug("Job deps attributes: %s" % deps_attributes[0].value)

        # submit job with (user) hold
        hold_attributes = pbs.new_attropl(1)
        hold_attributes[0].name = pbs.ATTR_h
        hold_attributes[0].value = pbs.USER_HOLD
        pbs_attributes.extend(hold_attributes)
        self.holds.append(pbs.USER_HOLD)
        self.log.debug("Job hold attributes: %s" % hold_attributes[0].value)

        # add a bunch of variables (added by qsub)
        # also set PBS_O_WORKDIR to os.getcwd()
        os.environ.setdefault('WORKDIR', os.getcwd())

        defvars = ['MAIL', 'HOME', 'PATH', 'SHELL', 'WORKDIR']
        pbsvars = ["PBS_O_%s=%s" % (x, os.environ.get(x, 'NOTFOUND_%s' % x)) for x in defvars]
        # extend PBS variables with specified variables
        pbsvars.extend(["%s=%s" % (name, value) for (name, value) in self.env_vars.items()])
        variable_attributes = pbs.new_attropl(1)
        variable_attributes[0].name = pbs.ATTR_v  # Variable_List
        variable_attributes[0].value = ",".join(pbsvars)

        pbs_attributes.extend(variable_attributes)
        self.log.debug("Job variable attributes: %s" % variable_attributes[0].value)

        # mail settings
        mail_attributes = pbs.new_attropl(1)
        mail_attributes[0].name = pbs.ATTR_m  # Mail_Points
        mail_attributes[0].value = 'n'  # disable all mail
        pbs_attributes.extend(mail_attributes)
        self.log.debug("Job mail attributes: %s" % mail_attributes[0].value)

        fh, scriptfn = tempfile.mkstemp()
        f = os.fdopen(fh, 'w')
        self.log.debug("Writing temporary job script to %s" % scriptfn)
        f.write(txt)
        f.close()

        self.log.debug("Going to submit to queue %s" % self.queue)

        # job submission sometimes fails without producing an error, e.g. when one of the dependency jobs has already finished
        # when that occurs, None will be returned by pbs_submit as job id
        jobid = pbs.pbs_submit(self.pbsconn, pbs_attributes, scriptfn, self.queue, NULL)
        is_error, errormsg = pbs.error()
        if is_error or jobid is None:
            self.log.error("Failed to submit job script %s (job id: %s, error %s)" % (scriptfn, jobid, errormsg))
        else:
            self.log.debug("Succesful job submission returned jobid %s" % jobid)
            self.jobid = jobid
            os.remove(scriptfn)

    def set_hold(self, hold_type=None):
        """Set hold on job of specified type."""
        # we can't set this default for hold_type in function signature,
        # because we need to be able to load this module even when the pbs module is not available
        if hold_type is None:
            hold_type = pbs.USER_HOLD
        # only set hold if it wasn't set before
        if hold_type not in self.holds:
            if hold_type not in KNOWN_HOLD_TYPES:
                self.log.error("set_hold: unknown hold type: %s (supported: %s)" % (hold_type, KNOWN_HOLD_TYPES))
            # set hold, check for errors, and keep track of this hold
            ec = pbs.pbs_holdjob(self.pbsconn, self.jobid, hold_type, NULL)
            is_error, errormsg = pbs.error()
            if is_error or ec:
                tup = (hold_type, self.jobid, is_error, ec, errormsg)
                self.log.error("Failed to set hold of type %s on job %s (is_error: %s, exit code: %s, msg: %s)" % tup)
            else:
                self.holds.append(hold_type)
        else:
            self.log.warning("Hold type %s was already set for %s" % (hold_type, self.jobid))

    def release_hold(self, hold_type=None):
        """Release hold on job of specified type."""
        # we can't set this default for hold_type in function signature,
        # because we need to be able to load this module even when the pbs module is not available
        if hold_type is None:
            hold_type = pbs.USER_HOLD
        # only release hold if it was set
        if hold_type in self.holds:
            if hold_type not in KNOWN_HOLD_TYPES:
                self.log.error("release_hold: unknown hold type: %s (supported: %s)" % (hold_type, KNOWN_HOLD_TYPES))
            # release hold, check for errors, remove from list of holds
            ec = pbs.pbs_rlsjob(self.pbsconn, self.jobid, hold_type, NULL)
            self.log.debug("Released hold of type %s for job %s" % (hold_type, self.jobid))
            is_error, errormsg = pbs.error()
            if is_error or ec:
                tup = (hold_type, self.jobid, is_error, ec, errormsg)
                self.log.error("Failed to release hold type %s on job %s (is_error: %s, exit code: %s, msg: %s)" % tup)
            else:
                self.holds.remove(hold_type)
        else:
            self.log.warning("No hold type %s was set for %s, so skipping hold release" % (hold_type, self.jobid))

    def has_holds(self):
        """Return whether this job has holds or not."""
        return bool(self.holds)

    def state(self):
        """
        Return the state of the job
        State can be 'not submitted', 'running', 'queued' or 'finished',
        """
        state = self.info(types=['job_state', 'exec_host'])

        if state == None:
            if self.jobid == None:
                return 'not submitted'
            else:
                return 'finished'

        jid = state['id']

        jstate = state.get('job_state', None)

        def get_uniq_hosts(txt, num=None):
            """
            - txt: format: host1/cpuid+host2/cpuid
            - num: number of nodes to return (default: all)
            """
            if num is None:
                num = -1
            res = []
            for h_c in txt.split('+'):
                h = h_c.split('/')[0]
                if h in res:
                    continue
                res.append(h)
            return res[:num]

        ehosts = get_uniq_hosts(state.get('exec_host', ''), 1)

        self.log.debug("Jobid %s jid %s state %s ehosts %s (%s)" % (self.jobid, jid, jstate, ehosts, state))
        if jstate == 'Q':
            return 'queued'
        else:
            return 'running'

    def info(self, types=None):
        """
        Return jobinfo
        """
        if not self.jobid:
            self.log.debug("no jobid, job is not submitted yet?")
            return None

        # convert single type into list
        if type(types) is str:
            types = [types]

        self.log.debug("Return info types %s" % types)

        # create attribute list to query pbs with
        if types is None:
            jobattr = NULL
        else:
            jobattr = pbs.new_attrl(len(types))
            for idx, attr in enumerate(types):
                jobattr[idx].name = attr

        jobs = pbs.pbs_statjob(self.pbsconn, self.jobid, jobattr, NULL)
        if len(jobs) == 0:
            # no job found, return None info
            res = None
            self.log.debug("No job found. Wrong id %s or job finished? Returning %s" % (self.jobid, res))
            return res
        elif len(jobs) == 1:
            self.log.debug("Request for jobid %s returned one result %s" % (self.jobid, jobs))
        else:
            self.log.error("Request for jobid %s returned more then one result %s" % (self.jobid, jobs))

        # only expect to have a list with one element
        j = jobs[0]
        # convert attribs into useable dict
        job_details = dict([ (attrib.name, attrib.value) for attrib in j.attribs ])
        # manually set 'id' attribute
        job_details['id'] = j.name
        self.log.debug("Found jobinfo %s" % job_details)
        return job_details

    def remove(self):
        """Remove the job with id jobid"""
        result = pbs.pbs_deljob(self.pbsconn, self.jobid, '')  # use empty string, not NULL
        if result:
            self.log.error("Failed to delete job %s: error %s" % (self.jobid, result))
        else:
            self.log.debug("Succesfully deleted job %s" % self.jobid)
