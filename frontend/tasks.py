"""
This file of part of CalcUS.

Copyright (C) 2020-2022 Raphaël Robidas

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""


from __future__ import absolute_import, unicode_literals

# from asyncio.windows_events import NULL

from calcus.celery import app
import string
import signal
import psutil
import redis
import requests
import os
import numpy as np
import decimal
import math
import subprocess
import shlex
import glob
import sys
import shutil
import invoke
import tempfile
import json

import threading
from threading import Lock

from shutil import copyfile, rmtree
import time
from celery.signals import task_prerun, task_postrun
from django.utils import timezone
from django.conf import settings
from django.core import management
from django.db.utils import IntegrityError
from celery.contrib.abortable import AbortableTask, AbortableAsyncResult
from celery import group

from ccinput.wrapper import generate_calculation
from ccinput.exceptions import CCInputException

from .libxyz import *
from .models import *
from .calculation_helper import *
from .environment_variables import *

import traceback
import periodictable

import logging

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s]  %(module)s: %(message)s"
)
logger = logging.getLogger(__name__)


REMOTE = False
connections = {}
locks = {}
remote_dirs = {}
kill_sig = []
cache_ind = 1


def direct_command(command, conn, lock, attempt_count=1):
    lock.acquire()

    def retry():
        lock.release()
        if attempt_count >= MAX_COMMAND_ATTEMPT_COUNT:
            logger.warning("Maximum number of command execution attempts reached!")
            return ErrorCodes.FAILED_TO_EXECUTE_COMMAND
        else:
            logger.warning(
                "Trying again to execute the command... (attempt {}/{})".format(
                    attempt_count, MAX_COMMAND_ATTEMPT_COUNT
                )
            )
            time.sleep(2)
            return direct_command(command, conn, lock, attempt_count + 1)

    # Do not run the actual calculation in a test
    if not is_test or (
        command.find("xtb") == -1
        and command.find("stda") == -1
        and command.find("crest") == -1
    ):
        try:
            response = conn[1].run("source ~/.bashrc; " + command, hide="both")
        except invoke.exceptions.UnexpectedExit as e:
            lock.release()
            if e.result.exited == 1 and (
                e.result.stderr.find("Invalid job id specified") != -1
                or e.result.stdout.find("Invalid job id specified") != -1
            ):
                return []

            logger.info(f"Command {command} terminated with exception: {e}")
            return []
        except ConnectionResetError as e:
            logger.debug("Connection reset while executing command")
            return retry()
        except TimeoutError as e:
            logger.debug("Connection timed out while executing command")
            return retry()
        except OSError as e:
            logger.debug("Socked closed while executing command")
            return retry()

    lock.release()

    return response.stdout.split("\n")


def sftp_get(src, dst, conn, lock, attempt_count=1):

    if (
        os.getenv("USE_CACHED_LOGS") == "true"
        and os.getenv("CAN_USE_CACHED_LOGS") == "true"
    ):
        return ErrorCodes.SUCCESS

    _src = str(src).replace("//", "/")
    _dst = str(dst).replace("//", "/")

    if _src.strip() == "":
        return ErrorCodes.INVALID_COMMAND

    system(f"mkdir -p {'/'.join(_dst.split('/')[:-1])}", force_local=True)

    lock.acquire()

    for i in range(3):
        try:
            conn[1].get(src, local=dst)
        except FileNotFoundError:
            logger.info(f"Could not download {src}: no such remote file")
            lock.release()
            return ErrorCodes.COULD_NOT_GET_REMOTE_FILE
        except ConnectionResetError as e:
            logger.debug("Connection reset while transferring file")
        except TimeoutError as e:
            logger.debug("Connection timed out while transferring file")
        except OSError as e:
            logger.debug("Socked closed while transferring file")
        else:
            ret = ErrorCodes.SUCCESS
            break
        time.sleep(1)
    else:
        ret = ErrorCodes.CONNECTION_ERROR

    lock.release()

    return ret


def sftp_put(src, dst, conn, lock, attempt_count=1):

    if not os.path.exists(src):
        return

    ret = direct_command(f"mkdir -p {'/'.join(dst.split('/')[:-1])}", conn, lock)

    lock.acquire()

    conn[1].put(src, remote=dst)

    lock.release()
    return ErrorCodes.SUCCESS


def wait_until_logfile(remote_dir, conn, lock):
    if is_test:
        DELAY = [2]
    else:
        DELAY = [5, 30, 60, 180, 300, 600]

    ind = 0
    while ind < len(DELAY):
        output = direct_command(f"ls {remote_dir}", conn, lock)
        if not isinstance(output, ErrorCodes):
            if len(output) == 1 and output[0].strip() == "":
                logger.info("Received nothing, ignoring")
            else:
                _output = [i for i in output if i.strip() != ""]
                for i in _output:
                    if i.find("CalcUS-") != -1 and i.find(".log") != -1:
                        job_id = i.replace("CalcUS-", "").replace(".log", "")
                        logger.debug(f"Log found: {job_id}")
                        return job_id
        time.sleep(DELAY[ind])
        ind += 1
    logger.warning("Failed to find a job log")
    return ErrorCodes.NO_JOB_LOG


def wait_until_done(calc, conn, lock, ind=0):
    job_id = calc.remote_id
    logger.info(f"Waiting for job {job_id} to finish")

    if is_test:
        DELAY = [2]
    else:
        DELAY = [5, 20, 30, 60, 120, 240, 600]

    pid = int(threading.get_ident())

    while True:
        output = direct_command(f"squeue -j {job_id}", conn, lock)
        _output = [i for i in output if i.strip() != ""]
        if not isinstance(output, ErrorCodes) and len(_output) > 1:
            logger.info(f"Waiting ({job_id})")
            try:
                status = _output[1].split()[4]
            except IndexError:
                logger.warning("Got unexpected str: " + str(_output))
                return

            if status == "R" and calc.status == 0:
                calc.date_started = timezone.now()
                calc.status = 1
                calc.save()
        elif isinstance(output, ErrorCodes):
            logger.warning(f"Could not check the status of job {job_id}")
        else:
            logger.info(f"Job done ({job_id})")
            return ErrorCodes.SUCCESS

        for i in range(DELAY[ind]):
            if pid in kill_sig:
                direct_command(f"scancel {job_id}", conn, lock)
                return ErrorCodes.JOB_CANCELLED

            if pid not in connections.keys():
                logger.info(f"Thread aborted for calculation {calc.id}")
                return ErrorCodes.SERVER_DISCONNECTED
            time.sleep(1)

        if ind < len(DELAY) - 1:
            ind += 1


def testing_delay_local(res):
    """
    Wait some extra time during some tests to simulate the job running, then return.
    This function should be called instead of returning success directly in order to
    allow tests to cancel the job (instead of finishing too fast).
    """
    wait = int(os.environ.get("CACHE_POST_WAIT", "0"))
    for i in range(wait):
        time.sleep(1)
        if res.is_aborted():
            logger.info(f"Stopping calculation after loading the cache")
            return ErrorCodes.JOB_CANCELLED

    return ErrorCodes.SUCCESS


def testing_delay_remote(calc_id):
    """
    Same as `testing_delay_local`, but for remote calculations.
    """
    wait = int(os.environ.get("CACHE_POST_WAIT", "0"))
    for i in range(wait):
        if pid in kill_sig:
            return ErrorCodes.JOB_CANCELLED

            if pid not in connections.keys():
                return ErrorCodes.SERVER_DISCONNECTED
        time.sleep(1)

    return ErrorCodes.SUCCESS


def system(command, log_file="", force_local=False, software="xtb", calc_id=-1):
    if REMOTE and not force_local:
        assert calc_id != -1

        calc = Calculation.objects.get(pk=calc_id)
        job_name = f"CalcUS-{calc.id}"

        pid = int(threading.get_ident())
        # Get the variables based on thread ID
        # These are already set by cluster_daemon when running
        conn = connections[pid]
        lock = locks[pid]
        remote_dir = remote_dirs[pid]

        if calc_id != -1 and is_test and setup_cached_calc(calc):
            return testing_delay_remote(calc_id)

        if calc.status == 0 and calc.remote_id == 0:

            if log_file != "":
                output = direct_command(
                    "cd {}; cp /home/{}/calcus/submit_{}.sh .; echo '{} | tee {}' >> submit_{}.sh; sbatch --job-name={} submit_{}.sh | tee calcus".format(
                        remote_dir,
                        conn[0].cluster_username,
                        software,
                        command,
                        log_file,
                        software,
                        job_name,
                        software,
                    ),
                    conn,
                    lock,
                    attempt_count=MAX_COMMAND_ATTEMPT_COUNT,  # Do not retry, since it might submit multiple times
                )
            else:
                output = direct_command(
                    "cd {}; cp /home/{}/calcus/submit_{}.sh .; echo '{}' >> submit_{}.sh; sbatch --job-name={} submit_{}.sh | tee calcus".format(
                        remote_dir,
                        conn[0].cluster_username,
                        software,
                        command,
                        software,
                        job_name,
                        software,
                    ),
                    conn,
                    lock,
                    attempt_count=MAX_COMMAND_ATTEMPT_COUNT,
                )

            if output == ErrorCodes.FAILED_TO_EXECUTE_COMMAND or len(output) < 2:
                if calc_id != -1:
                    ind = 0

                    while ind < 20:
                        output = direct_command(
                            f"cd {remote_dir}; cat calcus", conn, lock
                        )
                        if isinstance(output, int):
                            ind += 1
                            time.sleep(1)
                        else:
                            break
                    if not isinstance(output, ErrorCodes) and len(output) > 0:
                        if len(output) == 1 and output[0].strip() == "":
                            logger.info("Calcus file empty, waiting for a log file")
                            job_id = wait_until_logfile(remote_dir, conn, lock)
                            if isinstance(job_id, ErrorCodes):
                                return job_id
                            else:
                                calc.remote_id = int(job_id)
                                calc.save()
                                ret = wait_until_done(calc, conn, lock)
                                if ret == ErrorCodes.SUCCESS:
                                    return testing_delay_remote(calc.id)
                                return ret
                        else:
                            job_id = (
                                output[-2].replace("Submitted batch job", "").strip()
                            )

                            calc.remote_id = int(job_id)
                            calc.save()
                            ret = wait_until_done(calc, conn, lock)
                            if ret == ErrorCodes.SUCCESS:
                                return testing_delay_remote(calc.id)
                            return ret
                    else:
                        return output
                else:
                    logger.warning("Channel timed out and no calculation id is set")
                    return output
            else:
                if output[-2].find("Submitted batch job") != -1:
                    job_id = output[-2].replace("Submitted batch job", "").strip()
                    calc.remote_id = int(job_id)
                    calc.save()
                    ret = wait_until_done(calc, conn, lock)
                    if ret == ErrorCodes.SUCCESS:
                        return testing_delay_remote(calc.id)
                    return ret
                else:
                    return ErrorCodes.FAILED_SUBMISSION
        else:
            if is_test:
                ret = wait_until_done(calc, conn, lock)
            else:
                ret = wait_until_done(calc, conn, lock, ind=6)

            if ret == ErrorCodes.SUCCESS:
                return testing_delay_remote(calc.id)

            return ret
    else:  # Local
        if calc_id != -1:
            calc = Calculation.objects.get(pk=calc_id)
            calc.status = 1
            calc.date_started = timezone.now()
            calc.save()
            res = AbortableAsyncResult(calc.task_id)

        if log_file != "":
            stream = open(log_file, "w")
        else:
            stream = open("/dev/null", "w")

        if calc_id != -1 and is_test and setup_cached_calc(calc):
            return testing_delay_local(res)

        try:
            t = subprocess.Popen(shlex.split(command), stdout=stream, stderr=stream)
        except FileNotFoundError:
            logger.error(f'Could not run command "{command}" - executable not found')
            calc.error_message = f"{command.split()[0]} is not found"
            calc.date_finished = timezone.now()
            calc.save()
            return ErrorCodes.FAILED_TO_RUN_LOCAL_SOFTWARE

        while True:
            poll = t.poll()

            if not poll is None:
                if t.returncode == 0:
                    if calc_id != -1:
                        return testing_delay_local(res)
                    return ErrorCodes.SUCCESS
                else:
                    logger.info(f"Got returncode {t.returncode}")
                    return ErrorCodes.UNKNOWN_TERMINATION

            if calc_id != -1 and res.is_aborted():
                logger.info(f"Stopping calculation {calc_id}")
                signal_to_send = signal.SIGTERM

                parent = psutil.Process(t.pid)
                children = parent.children(recursive=True)
                for process in children:
                    process.send_signal(signal.SIGTERM)

                # t.kill()
                t.send_signal(signal_to_send)
                t.wait()

                return ErrorCodes.JOB_CANCELLED

            time.sleep(1)


def files_are_equal(f, input_file):
    with open(f) as ff:
        lines = ff.readlines()

    lines = [i.strip() for i in lines if i.strip() != ""]
    sinput = [i.strip() for i in input_file.split("\n") if i.strip() != ""]

    if len(sinput) != len(lines):
        return False

    for l1, l2 in zip(lines, sinput):
        if l1.find("nproc") != -1 and l2.find("nproc") != -1:
            continue

        if l1 != l2:
            return False

    return True


def get_cache_index(calc, cache_path):
    inputs = list(glob.glob(cache_path + "/*.input"))
    for f in inputs:
        if files_are_equal(f, calc.all_inputs):
            ind = ".".join(f.split("/")[-1].split(".")[:-1])
            return ind
    else:
        return -1


def calc_is_cached(calc):
    if (
        os.getenv("USE_CACHED_LOGS") == "true"
        and os.getenv("CAN_USE_CACHED_LOGS") == "true"
    ):
        if calc.local and not os.path.isdir(CALCUS_CACHE_HOME):
            logger.info(f"no cache")
            os.mkdir(CALCUS_CACHE_HOME)
            return False

        index = get_cache_index(calc, CALCUS_CACHE_HOME)

        if index == -1:
            logger.info(f"not found")
            return False

        return index
    else:
        return False


def setup_cached_calc(calc):
    index = calc_is_cached(calc)
    if not index:
        return False

    scr_path = f"{CALCUS_SCR_HOME}/{calc.id}"
    if os.path.isdir(scr_path):
        if os.path.islink(scr_path):
            # Likely already setup
            return True
        rmtree(scr_path)

    os.symlink(
        os.path.join(CALCUS_CACHE_HOME, index),
        scr_path,
    )
    logger.info(f"Using cache ({index})")
    return True


def generate_xyz_structure(drawing, inp, ext):
    if ext == "xyz":
        return inp
    elif ext == "mol":
        with tempfile.TemporaryDirectory() as d:
            if drawing:
                with open(f"{d}/inp.mol", "w") as out:
                    out.write(inp)
                a = system(
                    f"obabel {d}/inp.mol -O {d}/inp.xyz -h --gen3D",
                    force_local=True,
                )
                with open(f"{d}/inp.xyz") as f:
                    lines = f.readlines()
                    return clean_xyz("".join(lines))
            else:
                to_print = []
                for line in inp.split("\n")[4:]:
                    sline = line.split()
                    try:
                        a = int(sline[3])
                    except ValueError:
                        to_print.append(
                            "{} {} {} {}\n".format(
                                sline[3],
                                float(sline[0]),
                                float(sline[1]),
                                float(sline[2]),
                            )
                        )
                    else:
                        break
                num = len(to_print)
                _xyz = f"{num}\n"
                _xyz += "CalcUS\n"
                for line in to_print:
                    _xyz += line
                return clean_xyz(_xyz)
    elif ext in ["sdf", "mol2"]:
        with tempfile.TemporaryDirectory() as d:
            with open(f"{d}/inp.{ext}", "w") as out:
                out.write(inp.replace("&lt;", "<").replace("&gt;", ">"))
            a = system(
                f"obabel {d}/inp.{ext} -O {d}/inp.xyz",
                force_local=True,
            )

            with open(f"{d}/inp.xyz") as f:
                lines = f.readlines()
            return clean_xyz("\n".join([i.strip() for i in lines]))
    elif ext in ["log", "out"]:
        return get_Gaussian_xyz(
            inp
        )  # Will not work for ORCA, cclib should probably be used
    elif ext in ["com", "gjf"]:
        return get_xyz_from_Gaussian_input(inp)
    else:
        logger.error(f"The conversion of files with extension {ext} is not implemented")
        return ErrorCodes.UNIMPLEMENTED


def launch_xtb_calc(in_file, calc, files):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))
    folder = f"scratch/calcus/{calc.id}"

    if calc.input_file != "":
        with open(os.path.join(local_folder, "input"), "w") as out:
            out.write(calc.input_file)

    os.chdir(local_folder)

    if not calc.local:
        pid = int(threading.get_ident())
        conn = connections[pid]
        lock = locks[pid]
        remote_dir = remote_dirs[pid]

        if calc.remote_id == 0:
            sftp_put(
                f"{local_folder}/in.xyz",
                os.path.join(folder, "in.xyz"),
                conn,
                lock,
            )
        if calc.input_file != "":
            sftp_put(
                f"{local_folder}/input",
                os.path.join(folder, "input"),
                conn,
                lock,
            )
    else:
        os.chdir(local_folder)

    ret = system(calc.command, "calc.out", software="xtb", calc_id=calc.id)

    cancelled = False
    if ret != ErrorCodes.SUCCESS:
        if ret == ErrorCodes.JOB_CANCELLED:
            cancelled = True
        else:
            return ret

    if not calc.local:
        for f in files:
            a = sftp_get(
                f"{folder}/{f}",
                os.path.join(CALCUS_SCR_HOME, str(calc.id), f),
                conn,
                lock,
            )
            if not cancelled and a != ErrorCodes.SUCCESS:
                return a
        if not cancelled and not (
            os.getenv("USE_CACHED_LOGS") == "true"
            and os.getenv("CAN_USE_CACHED_LOGS") == "true"
        ):
            a = sftp_get(
                f"{folder}/NOT_CONVERGED",
                os.path.join(CALCUS_SCR_HOME, str(calc.id), "NOT_CONVERGED"),
                conn,
                lock,
            )

            if a != ErrorCodes.COULD_NOT_GET_REMOTE_FILE:
                return ErrorCodes.FAILED_TO_CONVERGE

    if not cancelled:
        for f in files:
            if not os.path.isfile(f"{local_folder}/{f}"):
                return ErrorCodes.COULD_NOT_GET_REMOTE_FILE

        """
        with open("{}/calc.out".format(local_folder)) as f:
            lines = f.readlines()
            for line in lines:
                if line.find("[WARNING] Runtime exception occurred") != -1:
                    return 1
        """
        # Frequency calculations on unoptimized geometries (or TSs) give an error
        if calc.step.creates_ensemble:
            analyse_opt(calc.id)

        return ErrorCodes.SUCCESS
    else:
        return ErrorCodes.JOB_CANCELLED


def xtb_opt(in_file, calc):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))

    ret = launch_xtb_calc(in_file, calc, ["calc.out", "xtbopt.xyz"])

    if ret != ErrorCodes.SUCCESS:
        return ret

    with open(f"{local_folder}/xtbopt.xyz") as f:
        lines = f.readlines()

    xyz_structure = clean_xyz("".join(lines))

    with open(f"{local_folder}/calc.out") as f:
        lines = f.readlines()
        ind = len(lines) - 1

        try:
            while lines[ind].find("TOTAL ENERGY") == -1:
                ind -= 1
            E = float(lines[ind].split()[3])
        except IndexError:
            logger.error(
                f"Could not parse the output of calculation {calc.id}: invalid format"
            )
            return ErrorCodes.INVALID_OUTPUT

    s = Structure.objects.get_or_create(
        parent_ensemble=calc.result_ensemble,
        xyz_structure=xyz_structure,
        number=calc.structure.number,
    )[0]
    s.degeneracy = 1
    prop = get_or_create(calc.parameters, s)
    prop.energy = E
    prop.geom = True
    s.save()
    prop.save()

    return ErrorCodes.SUCCESS


def xtb_mep(in_file, calc):
    folder = "/".join(in_file.split("/")[:-1])
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))
    local = calc.local

    with open(os.path.join(local_folder, "struct2.xyz"), "w") as out:
        out.write(calc.aux_structure.xyz_structure)

    ret = launch_orca_calc(in_file, calc, ["calc.out", "calc_MEP_trj.xyz"])

    if ret != ErrorCodes.SUCCESS:
        return ret

    with open(f"{local_folder}/calc_MEP_trj.xyz") as f:
        lines = f.readlines()

    num_atoms = lines[0]
    inds = []
    ind = 0
    while ind < len(lines) - 1:
        if lines[ind] == num_atoms:
            inds.append(ind)
        ind += 1
    inds.append(len(lines))

    properties = []
    for metaind, mol in enumerate(inds[:-1]):
        sline = lines[inds[metaind] + 1].strip().split()
        E = float(sline[-1])
        struct = "".join(
            [i.strip() + "\n" for i in lines[inds[metaind] : inds[metaind + 1]]]
        )

        r = Structure.objects.get_or_create(
            number=metaind + 1, parent_ensemble=calc.result_ensemble
        )[0]
        r.degeneracy = 1
        r.xyz_structure = clean_xyz(struct)
        r.save()

        prop = get_or_create(calc.parameters, r)
        prop.energy = E
        prop.geom = True
        properties.append(prop)

    Property.objects.bulk_update(properties, ["energy", "geom"])
    return ErrorCodes.SUCCESS


def xtb_sp(in_file, calc):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))

    ret = launch_xtb_calc(in_file, calc, ["calc.out"])

    if ret != ErrorCodes.SUCCESS:
        return ret

    with open(f"{local_folder}/calc.out") as f:
        lines = f.readlines()
        ind = len(lines) - 1

        try:
            while lines[ind].find("TOTAL ENERGY") == -1:
                ind -= 1
            E = float(lines[ind].split()[3])
        except IndexError:
            logger.error(
                f"Could not parse the output of calculation {calc.id}: invalid format"
            )
            return ErrorCodes.INVALID_OUTPUT

    prop = get_or_create(calc.parameters, calc.structure)
    prop.energy = E
    prop.save()
    return ErrorCodes.SUCCESS


def get_or_create(params, struct):
    try:
        return struct.properties.get(parameters___md5=params.md5)  # parameters._md5
    except Property.DoesNotExist:
        return Property.objects.create(parameters=params, parent_structure=struct)


def xtb_ts(in_file, calc):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))
    local = calc.local

    ret = launch_orca_calc(in_file, calc, ["calc.out", "calc.xyz"])

    if ret != ErrorCodes.SUCCESS:
        return ret

    with open(os.path.join(local_folder, "calc.xyz")) as f:
        lines = f.readlines()

    with open(os.path.join(local_folder, "calc.out")) as f:
        olines = f.readlines()
        ind = len(olines) - 1
        try:
            while olines[ind].find("FINAL SINGLE POINT ENERGY") == -1:
                ind -= 1
            E = float(olines[ind].split()[4])
        except IndexError:
            logger.error(
                f"Could not parse the output of calculation {calc.id}: invalid format"
            )
            return ErrorCodes.INVALID_OUTPUT

    s = Structure.objects.get_or_create(
        parent_ensemble=calc.result_ensemble,
        xyz_structure=clean_xyz("".join(lines)),
        number=calc.structure.number,
    )[0]
    s.degeneracy = calc.structure.degeneracy
    prop = get_or_create(calc.parameters, s)
    prop.energy = E
    prop.geom = True

    s.xyz_structure = "\n".join([i.strip() for i in lines])

    s.save()
    prop.save()
    return ErrorCodes.SUCCESS


def xtb_scan(in_file, calc):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))

    has_scan = "scan" in calc.constraints.lower()

    if has_scan:
        ret = launch_xtb_calc(in_file, calc, ["calc.out", "xtbscan.log"])
    else:
        ret = launch_xtb_calc(in_file, calc, ["calc.out", "xtbopt.xyz"])

    failed = False
    if ret != ErrorCodes.SUCCESS:
        if ret == ErrorCodes.SERVER_DISCONNECTED:
            return ret
        if has_scan:
            failed = True
        else:
            return ret

    if has_scan:
        if not os.path.isfile(f"{local_folder}/xtbscan.log"):
            return ErrorCodes.MISSING_FILE
        with open(os.path.join(local_folder, "xtbscan.log")) as f:
            lines = f.readlines()
            if len(lines) == 0:
                if failed:
                    print("no lines")
                    return ret

                return ErrorCodes.INVALID_FILE

            num_atoms = lines[0]
            inds = []
            ind = 0
            while ind < len(lines) - 1:
                if lines[ind] == num_atoms:
                    inds.append(ind)
                ind += 1
            inds.append(len(lines))

            min_E = 0

            """
                Since we can't get the keys of items created in bulk and we can't set a reference without first creating the objects, I haven't found a way to create both the structures and properties using bulk_update. This is still >2.5 times faster than the naive approach.
            """
            properties = []

            for metaind, mol in enumerate(inds[:-1]):
                r = Structure.objects.get_or_create(
                    parent_ensemble=calc.result_ensemble, number=metaind + 1
                )[0]
                r.degeneracy = 1

                sline = lines[inds[metaind] + 1].strip().split()
                en_index = sline.index("energy:")
                E = float(sline[en_index + 1])
                struct = "".join(
                    [i.strip() + "\n" for i in lines[inds[metaind] : inds[metaind + 1]]]
                )

                r.xyz_structure = clean_xyz(struct)

                r.save()

                prop = Property(parameters=calc.parameters)
                prop.parent_structure = r
                prop.energy = E
                prop.geom = True

                properties.append(prop)

            Property.objects.bulk_create(properties)
    else:
        with open(os.path.join(local_folder, "xtbopt.xyz")) as f:
            lines = f.readlines()
            r = Structure.objects.get_or_create(
                parent_ensemble=calc.result_ensemble, number=calc.structure.number
            )[0]
            r.xyz_structure = clean_xyz("".join(lines))

        with open(os.path.join(local_folder, "calc.out")) as f:
            lines = f.readlines()
            ind = len(lines) - 1

            try:
                while lines[ind].find("TOTAL ENERGY") == -1:
                    ind -= 1
                E = float(lines[ind].split()[3])
            except IndexError:
                logger.error(
                    f"Could not parse the output of calculation {calc.id}: invalid format"
                )
                return ErrorCodes.INVALID_OUTPUT

            prop = get_or_create(calc.parameters, r)
            prop.energy = E

            r.save()
            prop.save()
            calc.result_ensemble.structure_set.add(r)
            calc.result_ensemble.save()

    if not failed:
        return ErrorCodes.SUCCESS
    else:
        return ret


def xtb_freq(in_file, calc):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))

    ret = launch_xtb_calc(in_file, calc, ["calc.out", "vibspectrum", "g98.out"])

    if ret != ErrorCodes.SUCCESS:
        return ret

    with open(f"{local_folder}/calc.out") as f:
        lines = f.readlines()
        ind = len(lines) - 1

        try:
            while lines[ind].find("TOTAL ENERGY") == -1:
                ind -= 1
            E = float(lines[ind].split()[3])
            G = float(lines[ind + 2].split()[4])
        except IndexError:
            logger.error(
                f"Could not parse the output of calculation {calc.id}: invalid format"
            )
            return ErrorCodes.INVALID_OUTPUT

    vib_file = os.path.join(local_folder, "vibspectrum")

    prop = get_or_create(calc.parameters, calc.structure)

    if os.path.isfile(vib_file):
        with open(vib_file) as f:
            lines = f.readlines()

        vibs = []
        intensities = []
        for line in lines:
            if len(line.split()) > 4 and line[0] != "#":
                sline = line.split()
                try:
                    a = float(sline[1])
                    if a == 0.0:
                        continue
                except ValueError:
                    pass
                vib = float(line[20:33].strip())
                vibs.append(vib)
                try:
                    intensity = float(sline[3])
                except ValueError:
                    continue
                else:
                    intensities.append(intensity)
        if len(vibs) == len(intensities) and len(intensities) > 0:
            x = np.arange(500, 4000, 1)  # Wave number in cm^-1
            spectrum = plot_vibs(x, zip(vibs, intensities))
            data = "Wavenumber,Intensity\n"
            intensities = 1000 * np.array(intensities) / max(intensities)
            for _x, i in sorted((zip(list(x), spectrum)), reverse=True):
                data += f"-{_x:.1f},{i:.5f}\n"

            prop.ir_spectrum = data
            prop.freq_list = vibs

    prop.energy = E
    prop.free_energy = G

    lines = [i + "\n" for i in calc.structure.xyz_structure.split("\n")]
    num_atoms = int(lines[0].strip())
    lines = lines[2:]
    hess = []
    struct = []

    for line in lines:
        if line.strip() != "":
            a, x, y, z = line.strip().split()
            struct.append([a, float(x), float(y), float(z)])

    with open(os.path.join(local_folder, "g98.out")) as f:
        lines = f.readlines()
        ind = 0

        try:
            while lines[ind].find("Atom AN") == -1:
                ind += 1
        except IndexError:
            logger.error(
                f"Could not parse the output of calculation {calc.id}: invalid format"
            )
            return ErrorCodes.INVALID_OUTPUT

        ind += 1

        vibs = []
        while ind < len(lines) - 1:

            vib = []
            sline = lines[ind].split()
            num_vibs = int((len(sline) - 2) / 3)
            for i in range(num_vibs):
                vib.append([])
            while ind < len(lines) and len(lines[ind].split()) > 3:
                sline = lines[ind].split()
                n = sline[0].strip()
                Z = sline[1].strip()
                for i in range(num_vibs):
                    x, y, z = sline[2 + 3 * i : 5 + 3 * i]
                    vib[i].append([x, y, z])
                ind += 1
            for i in range(num_vibs):
                vibs.append(vib[i])
            while ind < len(lines) - 1 and lines[ind].find("Atom AN") == -1:
                ind += 1
            ind += 1

        for ind in range(len(vibs)):
            anim = f"{num_atoms}\nCalcUS\n"
            assert len(struct) == num_atoms
            for ind2, (a, x, y, z) in enumerate(struct):
                anim += "{} {:.4f} {:.4f} {:.4f} {} {} {}\n".format(
                    a, x, y, z, *vibs[ind][ind2]
                )

            prop.freq_animations.append(anim)

    prop.save()
    return ErrorCodes.SUCCESS


def crest(in_file, calc):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))

    ret = launch_xtb_calc(in_file, calc, ["calc.out", "crest_conformers.xyz"])

    if ret != ErrorCodes.SUCCESS:
        return ret

    with open(os.path.join(local_folder, "calc.out")) as f:
        lines = f.readlines()
        ind = len(lines) - 1

        try:
            while (
                lines[ind].find("total number unique points considered further") == -1
            ):
                ind -= 1
        except IndexError:
            logger.error(
                f"Could not parse the output of calculation {calc.id}: invalid format"
            )
            return ErrorCodes.INVALID_OUTPUT

        weighted_energy = 0.0
        ind += 1
        structures = []
        properties = []
        while lines[ind].find("T /K") == -1:
            sline = lines[ind].strip().split()
            if len(sline) == 8:
                energy = float(sline[2])
                number = int(sline[5])
                degeneracy = int(sline[6])

                r = Structure.objects.get_or_create(
                    parent_ensemble=calc.result_ensemble, number=number
                )[0]
                r.degeneracy = degeneracy
                structures.append(r)

            ind += 1

    with open(os.path.join(local_folder, "crest_conformers.xyz")) as f:
        lines = f.readlines()
        num_atoms = lines[0]
        inds = []
        ind = 0
        while ind < len(lines) - 1:
            if lines[ind] == num_atoms:
                inds.append(ind)
            ind += 1
        inds.append(len(lines))

        assert len(inds) - 1 == len(structures)
        for metaind, mol in enumerate(inds[:-1]):
            E = float(lines[inds[metaind] + 1].strip())
            raw_lines = lines[inds[metaind] : inds[metaind + 1]]
            clean_lines = raw_lines[:2]

            for l in raw_lines[2:]:
                clean_lines.append(clean_struct_line(l))

            struct = clean_xyz("".join([i.strip() + "\n" for i in clean_lines]))
            structures[metaind].xyz_structure = struct

            prop = get_or_create(calc.parameters, structures[metaind])
            prop.energy = E
            prop.geom = True
            properties.append(prop)

    Property.objects.bulk_update(properties, ["energy", "geom"])
    Structure.objects.bulk_update(structures, ["xyz_structure"])

    return ErrorCodes.SUCCESS


def clean_struct_line(line):
    a, x, y, z = line.split()
    return f"{LOWERCASE_ATOMIC_SYMBOLS[a.lower()]} {x} {y} {z}\n"


def launch_orca_calc(in_file, calc, files):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))
    folder = f"scratch/calcus/{calc.id}"

    with open(os.path.join(local_folder, "calc.inp"), "w") as out:
        out.write(calc.input_file)

    if not calc.local:
        pid = int(threading.get_ident())
        conn = connections[pid]
        lock = locks[pid]
        remote_dir = remote_dirs[pid]
        if calc.remote_id == 0:
            sftp_put(
                f"{local_folder}/calc.inp",
                os.path.join(folder, "calc.inp"),
                conn,
                lock,
            )
            if calc.step.name == "Minimum Energy Path":
                sftp_put(
                    f"{local_folder}/struct2.xyz",
                    os.path.join(folder, "struct2.xyz"),
                    conn,
                    lock,
                )

        ret = system(
            "$EBROOTORCA/orca calc.inp", "calc.out", software="ORCA", calc_id=calc.id
        )
    else:
        os.chdir(local_folder)
        ret = system(
            f"{EBROOTORCA}/orca calc.inp",
            "calc.out",
            software="ORCA",
            calc_id=calc.id,
        )

    cancelled = False
    if ret != ErrorCodes.SUCCESS:
        if ret == ErrorCodes.JOB_CANCELLED:
            cancelled = True
        else:
            return ret

    if not calc.local:
        for f in files:
            a = sftp_get(
                f"{folder}/{f}",
                os.path.join(CALCUS_SCR_HOME, str(calc.id), f),
                conn,
                lock,
            )
            if not cancelled and a != ErrorCodes.SUCCESS:
                return a

        if not cancelled and calc.parameters.software == "xtb":
            a = sftp_get(
                f"{folder}/NOT_CONVERGED",
                os.path.join(CALCUS_SCR_HOME, str(calc.id), "NOT_CONVERGED"),
                conn,
                lock,
            )
            if a != ErrorCodes.COULD_NOT_GET_REMOTE_FILE:
                return ErrorCodes.FAILED_TO_CONVERGE

    if not cancelled:
        for f in files:
            if not os.path.isfile(f"{local_folder}/{f}"):
                return ErrorCodes.COULD_NOT_GET_REMOTE_FILE

        return ErrorCodes.SUCCESS
    else:
        return ErrorCodes.JOB_CANCELLED


def orca_mo_gen(in_file, calc):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))

    ret = launch_orca_calc(
        in_file,
        calc,
        ["calc.out", "in-HOMO.cube", "in-LUMO.cube", "in-LUMOA.cube", "in-LUMOB.cube"],
    )

    if ret != ErrorCodes.SUCCESS:
        return ret

    with open(f"{local_folder}/calc.out") as f:
        lines = f.readlines()
        ind = len(lines) - 1

        try:
            while lines[ind].find("FINAL SINGLE POINT ENERGY") == -1:
                ind -= 1
            E = float(lines[ind].split()[4])
        except IndexError:
            logger.error(
                f"Could not parse the output of calculation {calc.id}: invalid format"
            )
            return ErrorCodes.INVALID_OUTPUT

    prop = get_or_create(calc.parameters, calc.structure)
    cubes = {}
    for mo in ["HOMO", "LUMO", "LUMOA", "LUMOB"]:
        path = os.path.join(local_folder, f"in-{mo}.cube")
        if not os.path.isfile(path):
            logger.error(f"Cube file {path} does not exist!")
            return ErrorCodes.MISSING_FILE
        with open(path) as f:
            cube = "".join(f.readlines())
        cubes[mo] = cube

    prop.energy = E
    prop.mo = json.dumps(cubes)
    prop.save()

    parse_orca_charges(calc, calc.structure)

    return ErrorCodes.SUCCESS


def orca_opt(in_file, calc):
    lines = [
        i + "\n"
        for i in clean_xyz(calc.structure.xyz_structure).split("\n")[2:]
        if i != ""
    ]

    if len(lines) == 1:  # Single atom
        s = Structure.objects.get_or_create(
            parent_ensemble=calc.result_ensemble,
            xyz_structure=calc.structure.xyz_structure,
            number=calc.structure.number,
        )[0]
        s.degeneracy = calc.structure.degeneracy
        s.save()
        calc.structure = s
        calc.step = BasicStep.objects.get(name="Single-Point Energy")
        calc.save()
        add_input_to_calc(calc)
        return orca_sp(in_file, calc)

    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))

    ret = launch_orca_calc(in_file, calc, ["calc.out", "calc.xyz"])

    if ret != ErrorCodes.SUCCESS:
        return ret

    with open(f"{local_folder}/calc.xyz") as f:
        lines = f.readlines()

    xyz_structure = clean_xyz("\n".join([i.strip() for i in lines]))

    with open(f"{local_folder}/calc.out") as f:
        lines = f.readlines()
        ind = len(lines) - 1

        try:
            while lines[ind].find("FINAL SINGLE POINT ENERGY") == -1:
                ind -= 1
            E = float(lines[ind].split()[4])
        except IndexError:
            logger.error(
                f"Could not parse the output of calculation {calc.id}: invalid format"
            )
            return ErrorCodes.INVALID_OUTPUT

    s = Structure.objects.get_or_create(
        parent_ensemble=calc.result_ensemble,
        xyz_structure=xyz_structure,
        number=calc.structure.number,
    )[0]
    s.degeneracy = calc.structure.degeneracy
    prop = get_or_create(calc.parameters, s)
    prop.energy = E
    prop.geom = True
    s.save()
    prop.save()

    parse_orca_charges(calc, s)

    return ErrorCodes.SUCCESS


def orca_sp(in_file, calc):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))

    ret = launch_orca_calc(in_file, calc, ["calc.out"])

    if ret != ErrorCodes.SUCCESS:
        return ret

    with open(f"{local_folder}/calc.out") as f:
        lines = f.readlines()
        ind = len(lines) - 1

        try:
            while lines[ind].find("FINAL SINGLE POINT ENERGY") == -1:
                ind -= 1
            E = float(lines[ind].split()[4])
        except IndexError:
            logger.error(
                f"Could not parse the output of calculation {calc.id}: invalid format"
            )
            return ErrorCodes.INVALID_OUTPUT

    prop = get_or_create(calc.parameters, calc.structure)
    prop.energy = E
    prop.save()

    parse_orca_charges(calc, calc.structure)

    return ErrorCodes.SUCCESS


def orca_ts(in_file, calc):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))

    ret = launch_orca_calc(in_file, calc, ["calc.out", "calc.xyz"])

    if ret != ErrorCodes.SUCCESS:
        return ret

    with open(f"{local_folder}/calc.xyz") as f:
        lines = f.readlines()
    xyz_structure = clean_xyz("\n".join([i.strip() for i in lines]))
    with open(f"{local_folder}/calc.out") as f:
        lines = f.readlines()
        ind = len(lines) - 1

        try:
            while lines[ind].find("FINAL SINGLE POINT ENERGY") == -1:
                ind -= 1
            E = float(lines[ind].split()[4])
        except IndexError:
            logger.error(
                f"Could not parse the output of calculation {calc.id}: invalid format"
            )
            return ErrorCodes.INVALID_OUTPUT

    s = Structure.objects.get_or_create(
        parent_ensemble=calc.result_ensemble,
        xyz_structure=xyz_structure,
        number=calc.structure.number,
    )[0]
    s.degeneracy = calc.structure.degeneracy
    prop = get_or_create(calc.parameters, s)
    prop.energy = E
    prop.geom = True
    s.save()
    prop.save()

    parse_orca_charges(calc, s)

    return ErrorCodes.SUCCESS


def orca_freq(in_file, calc):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))

    ret = launch_orca_calc(in_file, calc, ["calc.out"])

    if ret != ErrorCodes.SUCCESS:
        return ret

    with open(f"{local_folder}/calc.out") as f:
        lines = f.readlines()
        ind = len(lines) - 1

    try:
        while lines[ind].find("Final Gibbs free energy") == -1:
            ind -= 1

        G = float(lines[ind].split()[5])

        while lines[ind].find("FINAL SINGLE POINT ENERGY") == -1:
            ind -= 1

        E = float(lines[ind].split()[4])

        while lines[ind].find("IR SPECTRUM") == -1 and ind > 0:
            ind += 1
    except IndexError:
        logger.error(
            f"Could not parse the output of calculation {calc.id}: invalid format"
        )
        return ErrorCodes.INVALID_OUTPUT

    assert ind > 0

    ind += 6

    nums = []
    vibs = []
    intensities = []

    while lines[ind].strip() != "":
        sline = lines[ind].strip().split()
        num = sline[0].replace(":", "")
        nums.append(num)

        vibs.append(float(sline[1]))
        intensities.append(float(sline[2]))

        ind += 1

    x = np.arange(500, 4000, 1)  # Wave number in cm^-1
    spectrum = plot_vibs(x, zip(vibs, intensities))

    prop = get_or_create(calc.parameters, calc.structure)

    if len(intensities) > 0:
        ir_spectrum = "Wavenumber,Intensity\n"
        intensities = 1000 * np.array(intensities) / max(intensities)
        for _x, i in sorted((zip(list(x), spectrum)), reverse=True):
            ir_spectrum += f"-{_x:.1f},{i:.5f}\n"
        prop.ir_spectrum = ir_spectrum

    prop.energy = E
    prop.free_energy = G

    raw_lines = calc.structure.xyz_structure.split("\n")
    xyz_lines = []
    for line in raw_lines:
        if line.strip() != "":
            xyz_lines.append(line)

    num_atoms = int(xyz_lines[0].strip())
    xyz_lines = xyz_lines[2:]
    struct = []

    for line in xyz_lines:
        if line.strip() != "":
            a, x, y, z = line.strip().split()
            struct.append([a, float(x), float(y), float(z)])

    parse_orca_charges(calc, calc.structure)

    if num_atoms == 1:
        return ErrorCodes.SUCCESS

    while lines[ind].find("VIBRATIONAL FREQUENCIES") == -1 and ind > 0:
        ind -= 1

    assert ind > 0
    ind += 5

    vibs = []
    while lines[ind].strip() != "":
        sline = lines[ind].strip().split()
        num = sline[0].replace(":", "")
        val = float(sline[1])
        if val != 0.0:
            vibs.append(val)

        ind += 1

    prop.freq_list = vibs

    while lines[ind].find("NORMAL MODES") == -1 and ind < len(lines) - 1:
        ind += 1

    assert ind < len(lines) - 1

    ind += 7
    start_num = int(nums[0])
    end_num = int(nums[-1])

    vibs = []
    while lines[ind].strip() != "":
        num_line = len(lines[ind].strip().split())
        ind += 1

        vib = []
        for i in range(num_line):
            vib.append([])

        for i in range(end_num + 1):
            sline = lines[ind].split()
            for i in range(num_line):
                coord = float(sline[1 + i])
                vib[i].append(coord)
            ind += 1

        def is_all_null(arr):
            for el in arr:
                if float(el) != 0:
                    return False
            return True

        for v in vib:
            if not is_all_null(v):
                vibs += [v]

    freq_animations = []
    for ind in range(len(vibs)):
        anim = f"{num_atoms}\nCalcUS\n"
        assert len(struct) == num_atoms
        for ind2, (a, x, y, z) in enumerate(struct):
            anim += "{} {:.4f} {:.4f} {:.4f} {} {} {}\n".format(
                a, x, y, z, *vibs[ind][3 * ind2 : 3 * ind2 + 3]
            )
        freq_animations.append(anim)

    prop.freq_animations = freq_animations
    prop.save()
    return ErrorCodes.SUCCESS


def orca_scan(in_file, calc):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))

    has_scan = "scan" in calc.constraints.lower()

    if has_scan:
        ret = launch_orca_calc(
            in_file,
            calc,
            ["calc.out", "calc.relaxscanact.dat", "calc.allxyz", "calc_trj.xyz"],
        )
    else:
        ret = launch_orca_calc(in_file, calc, ["calc.out", "calc.xyz"])

    if ret != ErrorCodes.SUCCESS:
        return ret

    if has_scan:
        properties = []
        energies = []
        with open(os.path.join(local_folder, "calc.relaxscanact.dat")) as f:
            lines = f.readlines()
            for line in lines:
                energies.append(float(line.split()[1]))
        with open(os.path.join(local_folder, "calc.allxyz")) as f:
            lines = f.readlines()
            num_atoms = lines[0]
            inds = []
            ind = 0
            try:
                while ind < len(lines) - 1:
                    if lines[ind] == num_atoms:
                        inds.append(ind)
                    ind += 1
            except IndexError:
                logger.error(
                    f"Could not parse the output of calculation {calc.id}: invalid format"
                )
                return ErrorCodes.INVALID_OUTPUT

            inds.append(len(lines) + 1)

            min_E = 0
            for metaind, mol in enumerate(inds[:-1]):
                E = energies[metaind]
                struct = clean_xyz(
                    "".join(
                        [
                            i.strip() + "\n"
                            for i in lines[inds[metaind] : inds[metaind + 1] - 1]
                        ]
                    )
                )

                r = Structure.objects.get_or_create(
                    parent_ensemble=calc.result_ensemble, number=metaind + 1
                )[0]
                r.degeneracy = 1
                r.xyz_structure = struct
                r.save()

                prop = Property(parameters=calc.parameters, parent_structure=r)
                prop.energy = E
                prop.geom = True
                properties.append(prop)

        Property.objects.bulk_create(properties)
    else:
        with open(os.path.join(local_folder, "calc.xyz")) as f:
            lines = f.readlines()
            r = Structure.objects.get_or_create(
                parent_ensemble=calc.result_ensemble, number=calc.structure.number
            )[0]
            r.xyz_structure = clean_xyz("".join([i.strip() + "\n" for i in lines]))

        with open(os.path.join(local_folder, "calc.out")) as f:
            lines = f.readlines()
            ind = len(lines) - 1
            try:
                while lines[ind].find("FINAL SINGLE POINT ENERGY") == -1:
                    ind -= 1
                E = float(lines[ind].split()[4])
            except IndexError:
                logger.error(
                    f"Could not parse the output of calculation {calc.id}: invalid format"
                )
                return ErrorCodes.INVALID_OUTPUT

            prop = get_or_create(calc.parameters, r)
            prop.energy = E
            r.save()
            prop.save()

    ###CHARGES
    # Start_ind?

    return ErrorCodes.SUCCESS


def orca_nmr(in_file, calc):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))

    ret = launch_orca_calc(in_file, calc, ["calc.out"])

    if ret != ErrorCodes.SUCCESS:
        return ret

    with open(os.path.join(local_folder, "calc.out")) as f:
        lines = f.readlines()

    try:
        ind = len(lines) - 1
        while lines[ind].find("CHEMICAL SHIELDING SUMMARY (ppm)") == -1:
            ind -= 1
    except IndexError:
        logger.error(
            f"Could not parse the output of calculation {calc.id}: invalid format"
        )
        return ErrorCodes.INVALID_OUTPUT

    nmr = ""
    ind += 6
    while lines[ind].strip() != "":
        n, a, iso, an = lines[ind].strip().split()
        nmr += f"{int(n) + 1} {a} {iso}\n"
        ind += 1

    prop = get_or_create(calc.parameters, calc.structure)
    prop.simple_nmr = nmr

    while lines[ind].find("FINAL SINGLE POINT ENERGY") == -1:
        ind -= 1
    E = float(lines[ind].split()[4])
    prop.energy = E
    prop.save()

    parse_orca_charges(calc, calc.structure)

    return ErrorCodes.SUCCESS


FACTOR = 1
SIGMA = 0.2
SIGMA_L = 6199.21
E = 4.4803204e-10
NA = 6.02214199e23
C = 299792458
HC = 4.135668e15 * C
ME = 9.10938e-31

FUZZ_INT = 1.0 / 30
FUZZ_WIDTH = 50000


def plot_peaks(_x, PP):
    val = 0
    for w, T in PP:
        val += (
            np.sqrt(np.pi)
            * E**2
            * NA
            / (1000 * np.log(10) * C**2 * ME)
            * T
            / SIGMA
            * np.exp(-(((HC / _x - HC / w) / (HC / SIGMA_L)) ** 2))
        )
    return val


def plot_vibs(_x, PP):
    val = 0
    for w, T in PP:
        val += FUZZ_INT * (1 - np.exp(-((FUZZ_WIDTH / w - FUZZ_WIDTH / _x) ** 2)))
    return val


def xtb_stda(in_file, calc):  # TO OPTIMIZE

    ww = []
    TT = []
    PP = []

    folder = "/".join(in_file.split("/")[:-1])
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))
    local = calc.local

    if calc.parameters.solvent != "Vacuum":
        solvent_add = f"-g {SOLVENT_TABLE[calc.parameters.solvent]}"
    else:
        solvent_add = ""

    os.chdir(local_folder)
    ret1 = system(
        f"xtb4stda {in_file} -chrg {calc.parameters.charge} {solvent_add}",
        "calc.out",
        calc_id=calc.id,
    )

    if ret1 != ErrorCodes.SUCCESS:
        return ret1

    ret2 = system(
        "stda -xtb -e 12".format(in_file, calc.parameters.charge, solvent_add),
        "calc2.out",
        calc_id=calc.id,
    )

    if ret2 != ErrorCodes.SUCCESS:
        return ret2

    if not local:
        pid = int(threading.get_ident())
        conn = connections[pid]
        lock = locks[pid]
        remote_dir = remote_dirs[pid]

        a = sftp_get(
            f"{folder}/tda.dat",
            os.path.join(CALCUS_SCR_HOME, str(calc.id), "tda.dat"),
            conn,
            lock,
        )
        b = sftp_get(
            f"{folder}/calc.out",
            os.path.join(CALCUS_SCR_HOME, str(calc.id), "calc.out"),
            conn,
            lock,
        )
        c = sftp_get(
            f"{folder}/calc2.out",
            os.path.join(CALCUS_SCR_HOME, str(calc.id), "calc2.out"),
            conn,
            lock,
        )

        if a != ErrorCodes.SUCCESS:
            return a

        if b != ErrorCodes.SUCCESS:
            return b

        if c != ErrorCodes.SUCCESS:
            return c

    f_x = np.arange(120.0, 1200.0, 1.0)

    if not os.path.isfile(f"{local_folder}/tda.dat"):
        return ErrorCodes.COULD_NOT_GET_REMOTE_FILE

    with open(f"{local_folder}/tda.dat") as f:
        lines = f.readlines()

    ind = 0
    while lines[ind].find("DATXY") == -1:
        ind += 1
    ind += 1
    for line in lines[ind:]:
        n, ev, I, _x, _y, _z = line.split()
        ev = float(ev)
        I = float(I)
        ww.append(1240 / ev)
        TT.append(I)
    PP = sorted(zip(ww, TT), key=lambda i: i[1], reverse=True)
    yy = plot_peaks(f_x, PP)
    yy = np.array(yy) / max(yy)

    uvvis = "Wavelength (nm), Absorbance\n"
    for ind, x in enumerate(f_x):
        uvvis += f"{x},{yy[ind]:.8f}\n"

    prop = get_or_create(calc.parameters, calc.structure)
    prop.uvvis = uvvis
    prop.save()

    return ErrorCodes.SUCCESS


def calc_to_ccinput(calc):
    if calc.parameters.method != "":
        _method = calc.parameters.method
    elif calc.parameters.theory_level.lower() == "hf":
        _method = "HF"
    elif calc.parameters.theory_level.lower() == "ri-mp2":
        _method = "RI-MP2"
    else:
        raise Exception(
            f"No method specified; theory level is {calc.parameters.theory_level}"
        )
    _specifications = calc.parameters.specifications
    software = calc.parameters.software.lower()

    if software in ["gaussian", "orca"]:
        _specifications += " " + getattr(calc.order.author, "default_" + software)
        _solvation_radii = calc.parameters.solvation_radii
    else:
        _solvation_radii = ""

    try:
        _PAL = int(PAL)
    except ValueError:
        _PAL = 1

    try:
        _MEM = int(MEM)
    except ValueError:
        _MEM = 1000

    if is_test:
        _nproc = min(4, _PAL)
        _mem = 2000
    else:
        if calc.local:
            _nproc = _PAL
            _mem = _MEM
        else:
            _nproc = calc.order.resource.pal
            _mem = calc.order.resource.memory

    params = {
        "software": calc.parameters.software,
        "type": calc.step.name,
        "method": _method,
        "basis_set": calc.parameters.basis_set,
        "solvent": calc.parameters.solvent,
        "solvation_model": calc.parameters.solvation_model,
        "solvation_radii": _solvation_radii,
        "specifications": _specifications,
        "density_fitting": calc.parameters.density_fitting,
        "custom_basis_sets": calc.parameters.custom_basis_sets,
        "xyz": calc.structure.xyz_structure,
        "constraints": calc.constraints,
        "nproc": _nproc,
        "mem": _mem,
        "charge": calc.parameters.charge,
        "multiplicity": calc.parameters.multiplicity,
        "aux_name": "struct2",
        "name": "in",
    }
    try:
        inp = generate_calculation(**params)
    except CCInputException as e:
        return e

    return inp


def launch_gaussian_calc(in_file, calc, files):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))
    folder = f"scratch/calcus/{calc.id}"

    with open(os.path.join(local_folder, "calc.com"), "w") as out:  ###
        out.write(calc.input_file)

    if not calc.local:
        pid = int(threading.get_ident())
        conn = connections[pid]
        lock = locks[pid]
        remote_dir = remote_dirs[pid]

        if calc.remote_id == 0:
            sftp_put(
                f"{local_folder}/calc.com",
                os.path.join(folder, "calc.com"),
                conn,
                lock,
            )
        ret = system("g16 calc.com", software="Gaussian", calc_id=calc.id)
    else:
        os.chdir(local_folder)
        ret = system("g16 calc.com", software="Gaussian", calc_id=calc.id)

    cancelled = False
    if ret != ErrorCodes.SUCCESS:
        if ret == ErrorCodes.JOB_CANCELLED:
            cancelled = True
        else:
            return ret

    if not calc.local:
        for f in files:
            a = sftp_get(f"{folder}/{f}", os.path.join(local_folder, f), conn, lock)
            if not cancelled and a != ErrorCodes.SUCCESS:
                return a

    if not cancelled:
        for f in files:
            if not os.path.isfile(f"{local_folder}/{f}"):
                return ErrorCodes.COULD_NOT_GET_REMOTE_FILE

        with open(os.path.join(local_folder, "calc.log")) as f:
            lines = f.readlines()
            if lines[-1].find("Normal termination") == -1:
                return ErrorCodes.UNKNOWN_TERMINATION

        return ErrorCodes.SUCCESS
    else:
        return ErrorCodes.JOB_CANCELLED


def parse_orca_charges(calc, s):

    xyz = parse_xyz_from_text(calc.structure.xyz_structure)

    if len(xyz) < 2:  # Monoatomic
        return

    parse_default_orca_charges(calc, s)

    if calc.parameters.specifications.lower().replace("_", "").find("hirshfeld") != -1:
        parse_hirshfeld_orca_charges(calc, s)


def parse_hirshfeld_orca_charges(calc, s):
    prop = get_or_create(calc.parameters, s)

    with open(os.path.join(CALCUS_SCR_HOME, str(calc.id), "calc.out")) as f:
        lines = f.readlines()
    ind = len(lines) - 1

    try:
        while lines[ind].find("HIRSHFELD ANALYSIS") == -1:
            ind -= 1
    except IndexError:
        logger.error(
            f"Could not parse the Hirshfeld charges of calculation {calc.id}: invalid format"
        )
        return ErrorCodes.INVALID_OUTPUT

    ind += 7

    charges = []
    while lines[ind].strip() != "":
        n, a, chrg, spin = lines[ind].split()
        charges.append(f"{float(chrg):.2f}")
        ind += 1

    prop.charges += f"Hirshfeld:{','.join(charges)};"
    prop.save()


def parse_default_orca_charges(calc, s):
    prop = get_or_create(calc.parameters, s)

    with open(os.path.join(CALCUS_SCR_HOME, str(calc.id), "calc.out")) as f:
        lines = f.readlines()
    ind = len(lines) - 1

    try:
        while lines[ind].find("MULLIKEN ATOMIC CHARGES") == -1:
            ind -= 1
    except IndexError:
        logger.error(
            f"Could not parse the Mulliken charges of calculation {calc.id}: invalid format"
        )
        return ErrorCodes.INVALID_OUTPUT

    ind += 2

    charges = []
    while lines[ind].find("Sum of atomic charges:") == -1:
        chrg = lines[ind].split()[-1]
        charges.append(f"{float(chrg):.2f}")
        ind += 1

    prop.charges += f"Mulliken:{','.join(charges)};"

    try:
        while lines[ind].find("LOEWDIN ATOMIC CHARGES") == -1:
            ind -= 1
    except IndexError:
        logger.error(
            f"Could not parse the Löwdin charges of calculation {calc.id}: invalid format"
        )
        return ErrorCodes.INVALID_OUTPUT

    ind += 2

    charges = []
    while lines[ind].strip() != "":
        chrg = lines[ind].split()[-1]
        charges.append(f"{float(chrg):.2f}")
        ind += 1

    prop.charges += f"Loewdin:{','.join(charges)};"
    prop.save()


def parse_gaussian_charges(calc, s):
    parse_default_gaussian_charges(calc, s)
    for spec in calc.parameters.specifications.replace(", ", ",").split(
        " "
    ):  # The specifications have been cleaned/formatted already
        if spec.strip() == "":
            continue
        if spec.find("(") != -1:
            key, opt_str = spec.split("(")
            options = [i.strip().lower() for i in opt_str.replace(")", "").split(",")]
            if key == "pop":
                for option in options:
                    if option == "nbo" or option == "npa":
                        parse_NPA_gaussian_charges(calc, s)
                    elif option == "hirshfeld":
                        parse_Hirshfeld_gaussian_charges(calc, s)
                    elif option == "esp":
                        parse_ESP_gaussian_charges(calc, s)
                    elif option == "hly":
                        parse_HLY_gaussian_charges(calc, s)


def parse_default_gaussian_charges(calc, s):
    prop = get_or_create(calc.parameters, s)

    with open(os.path.join(CALCUS_SCR_HOME, str(calc.id), "calc.log")) as f:
        lines = f.readlines()
    ind = len(lines) - 1
    try:
        while lines[ind].find("Mulliken charges:") == -1:
            ind -= 1
    except IndexError:  # Monoatomic systems may not have charges
        return
    ind += 2
    charges = []
    while lines[ind].find("Sum of Mulliken charges") == -1:
        n, a, chrg = lines[ind].split()
        charges.append(f"{float(chrg):.2f}")
        ind += 1

    prop.charges += f"Mulliken:{','.join(charges)};"

    try:
        while lines[ind].find("APT charges:") == -1:
            ind += 1
    except IndexError:
        logger.warning(
            f"Could not parse the APT charges of calculation {calc.id}: invalid format"
        )
        pass
    else:
        ind += 2
        charges = []
        while lines[ind].find("Sum of APT charges") == -1:
            n, a, chrg = lines[ind].split()
            charges.append(f"{float(chrg):.2f}")
            ind += 1
        prop.charges += f"APT:{','.join(charges)};"

    prop.save()


def parse_ESP_gaussian_charges(calc, s):
    prop = get_or_create(calc.parameters, s)

    with open(os.path.join(CALCUS_SCR_HOME, str(calc.id), "calc.log")) as f:
        lines = f.readlines()
    ind = len(lines) - 1

    try:
        while lines[ind].find("ESP charges:") == -1:
            ind -= 1
        ind += 2
        charges = []
        while lines[ind].find("Sum of ESP charges") == -1:
            a, n, chrg, *_ = lines[ind].split()
            charges.append(f"{float(chrg):.2f}")
            ind += 1
    except IndexError:
        logger.error(
            f"Could not parse the ESP charges of calculation {calc.id}: invalid format"
        )
        return ErrorCodes.INVALID_OUTPUT

    prop.charges += f"ESP:{','.join(charges)};"
    prop.save()


def parse_HLY_gaussian_charges(calc, s):
    prop = get_or_create(calc.parameters, s)

    with open(os.path.join(CALCUS_SCR_HOME, str(calc.id), "calc.log")) as f:
        lines = f.readlines()
    ind = len(lines) - 1
    while (
        lines[ind].find(
            "Generate Potential Derived Charges using the Hu-Lu-Yang model:"
        )
        == -1
    ):
        ind -= 1

    try:
        while lines[ind].find("ESP charges:") == -1:
            ind += 1
    except IndexError:
        logger.error(
            f"Could not parse the ESP charges of calculation {calc.id}: invalid format"
        )
        return ErrorCodes.INVALID_OUTPUT

    ind += 2
    charges = []
    while lines[ind].find("Sum of ESP charges") == -1:
        a, n, chrg, *_ = lines[ind].split()
        charges.append(f"{float(chrg):.2f}")
        ind += 1

    prop.charges += f"HLY:{','.join(charges)};"
    prop.save()


def parse_NPA_gaussian_charges(calc, s):
    prop = get_or_create(calc.parameters, s)

    with open(os.path.join(CALCUS_SCR_HOME, str(calc.id), "calc.log")) as f:
        lines = f.readlines()
    ind = len(lines) - 1
    while lines[ind].find("Summary of Natural Population Analysis:") == -1:
        ind -= 1
    ind += 6
    charges = []
    while lines[ind].find("===========") == -1:
        a, n, chrg, *_ = lines[ind].split()
        charges.append(f"{float(chrg):.2f}")
        ind += 1

    prop.charges += f"NBO:{','.join(charges)};"
    prop.save()


def parse_Hirshfeld_gaussian_charges(calc, s):
    prop = get_or_create(calc.parameters, s)

    with open(os.path.join(CALCUS_SCR_HOME, str(calc.id), "calc.log")) as f:
        lines = f.readlines()
    ind = len(lines) - 1
    try:
        while (
            lines[ind].find(
                "Hirshfeld charges, spin densities, dipoles, and CM5 charges"
            )
            == -1
        ):
            ind -= 1
    except IndexError:
        logger.error(
            f"Could not parse the Hirshfeld charges of calculation {calc.id}: invalid format"
        )
        return ErrorCodes.INVALID_OUTPUT

    ind += 2
    charges_hirshfeld = []
    charges_CM5 = []
    while lines[ind].find("Tot") == -1:
        a, n, hirshfeld, _, _, _, _, CM5 = lines[ind].split()
        charges_hirshfeld.append(f"{float(hirshfeld):.2f}")
        charges_CM5.append(f"{float(CM5):.2f}")
        ind += 1

    prop.charges += f"Hirshfeld:{','.join(charges_hirshfeld)};"
    prop.charges += f"CM5:{','.join(charges_CM5)};"
    prop.save()


def gaussian_sp(in_file, calc):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))

    ret = launch_gaussian_calc(in_file, calc, ["calc.log"])

    if ret != ErrorCodes.SUCCESS:
        return ret

    with open(f"{local_folder}/calc.log") as f:
        lines = f.readlines()
        ind = len(lines) - 1

        while lines[ind].find("SCF Done") == -1:
            ind -= 1
        E = float(lines[ind].split()[4])

    parse_gaussian_charges(calc, calc.structure)

    prop = get_or_create(calc.parameters, calc.structure)
    prop.energy = E
    prop.save()

    return ErrorCodes.SUCCESS


def gaussian_td(in_file, calc):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))

    ret = launch_gaussian_calc(in_file, calc, ["calc.log"])

    if ret != ErrorCodes.SUCCESS:
        return ret

    wavenumbers = []
    intensities = []
    with open(f"{local_folder}/calc.log") as f:
        lines = f.readlines()
        ind = len(lines) - 1

        try:
            while lines[ind].find("SCF Done") == -1:
                ind -= 1
            E = float(lines[ind].split()[4])

            while (
                lines[ind].find("Excitation energies and oscillator strengths:") == -1
            ):
                ind += 1
        except IndexError:
            logger.error(
                f"Could not parse the output of calculation {calc.id}: invalid format"
            )
            return ErrorCodes.INVALID_OUTPUT

        def parse_td_dft(lines, ind):
            while lines[ind].find("Leave Link  914") == -1:
                while lines[ind].find("<S**2>=") == -1:
                    ind += 1
                    if lines[ind].find("Leave Link  914") != -1:
                        return
                sline = lines[ind].split()
                ev = sline[4]
                intensity = sline[8][3:]
                try:
                    ev = float(ev)
                    intensity = float(intensity)
                except ValueError:
                    logging.warning(
                        "Gaussian TD-DFT output does not have the expected format! Got excitation energy '{}' and intensity '{}'".format(
                            ev, intensity
                        )
                    )
                    continue
                wavenumbers.append(1240 / ev)
                intensities.append(intensity)
                ind += 1

    parse_td_dft(lines, ind)

    parse_gaussian_charges(calc, calc.structure)

    f_x = np.arange(120.0, 1200.0, 1.0)

    PP = sorted(zip(wavenumbers, intensities), key=lambda i: i[1], reverse=True)
    yy = plot_peaks(f_x, PP)
    yy = np.array(yy) / max(yy)

    uvvis = "Wavelength (nm), Absorbance\n"
    for ind, x in enumerate(f_x):
        uvvis += f"{x},{yy[ind]:.8f}\n"

    prop = get_or_create(calc.parameters, calc.structure)
    prop.uvvis = uvvis
    prop.energy = E
    prop.save()

    return ErrorCodes.SUCCESS


def gaussian_opt(in_file, calc):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))

    ret = launch_gaussian_calc(in_file, calc, ["calc.log"])

    if ret != ErrorCodes.SUCCESS:
        return ret

    with open(f"{local_folder}/calc.log") as f:
        lines = f.readlines()
        ind = len(lines) - 1

        try:
            while lines[ind].find("SCF Done") == -1:
                ind -= 1
            E = float(lines[ind].split()[4])
            while (
                lines[ind].find(
                    "Center     Atomic      Atomic             Coordinates (Angstroms)"
                )
                == -1
            ):
                ind += 1
            ind += 3
        except IndexError:
            logger.error(
                f"Could not parse the output of calculation {calc.id}: invalid format"
            )
            return ErrorCodes.INVALID_OUTPUT

        xyz = []
        while lines[ind].find("----") == -1:
            n, a, t, x, y, z = lines[ind].strip().split()
            xyz.append([ATOMIC_SYMBOL[int(a)], x, y, z])
            ind += 1

        xyz_structure = f"{len(xyz)}\nCalcUS\n"
        for el in xyz:
            xyz_structure += "{} {} {} {}\n".format(*el)

        xyz_structure = clean_xyz(xyz_structure)

    s = Structure.objects.get_or_create(
        parent_ensemble=calc.result_ensemble,
        xyz_structure=xyz_structure,
        number=calc.structure.number,
    )[0]
    s.degeneracy = calc.structure.degeneracy
    prop = get_or_create(calc.parameters, s)
    prop.energy = E
    prop.geom = True
    s.save()
    prop.save()

    parse_gaussian_charges(calc, s)
    return ErrorCodes.SUCCESS


def gaussian_freq(in_file, calc):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))

    ret = launch_gaussian_calc(in_file, calc, ["calc.log"])

    if ret != ErrorCodes.SUCCESS:
        return ret

    try:
        with open(f"{local_folder}/calc.log") as f:
            outlines = f.readlines()
            ind = len(outlines) - 1

        while outlines[ind].find("Zero-point correction") == -1:
            ind -= 1

        ZPE = outlines[ind].split()[-2]
        H = outlines[ind + 2].split()[-1]
        G = outlines[ind + 3].split()[-1]

        while outlines[ind].find("SCF Done") == -1:
            ind -= 1

        SCF = outlines[ind].split()[4]
    except IndexError:
        logger.error(
            f"Could not parse the output of calculation {calc.id}: invalid format"
        )
        return ErrorCodes.INVALID_OUTPUT

    prop = get_or_create(calc.parameters, calc.structure)
    prop.energy = SCF
    prop.free_energy = float(0.0030119 + float(G) + float(SCF))

    try:
        while outlines[ind].find("Standard orientation:") == -1:
            ind -= 1
        ind += 5

    except IndexError:  # "Standard orientation" is not in all Gaussian output files, apparently
        ind = 0

        raw_lines = calc.structure.xyz_structure.split("\n")
        xyz_lines = []
        for line in raw_lines:
            if line.strip() != "":
                xyz_lines.append(line)

        num_atoms = int(xyz_lines[0].strip())
        xyz_lines = xyz_lines[2:]
        struct = []
        for line in xyz_lines:
            if line.strip() != "":
                a, x, y, z = line.strip().split()
                struct.append([a, float(x), float(y), float(z)])
    else:
        struct = []
        while outlines[ind].find("-----------") == -1:
            _, n, _, x, y, z = outlines[ind].split()
            a = ATOMIC_SYMBOL[int(n)]
            struct.append([a, float(x), float(y), float(z)])
            ind += 1
        num_atoms = len(struct)

    while outlines[ind].find("and normal coordinates:") == -1:
        ind += 1
    ind += 3

    if outlines[ind].find("Thermochemistry") != -1:  # No vibration
        return ErrorCodes.SUCCESS

    vibs = []
    wavenumbers = []
    intensities = []
    while ind < len(outlines) - 1:
        vib = []
        intensity = []
        sline = outlines[ind].split()
        num_vibs = int((len(sline) - 2))

        for i in range(num_vibs):
            wavenumbers.append(float(sline[2 + i]))
            intensities.append(float(outlines[ind + 3].split()[3 + i]))
            vib.append([])

        while outlines[ind].find("Atom  AN") == -1:
            ind += 1

        ind += 1

        while ind < len(outlines) and len(outlines[ind].split()) > 3:
            sline = outlines[ind].split()
            n = sline[0].strip()
            Z = sline[1].strip()
            for i in range(num_vibs):
                x, y, z = sline[2 + 3 * i : 5 + 3 * i]
                vib[i].append([x, y, z])
            ind += 1
        for i in range(num_vibs):
            vibs.append(vib[i])
        while ind < len(outlines) - 1 and outlines[ind].find("Frequencies --") == -1:
            ind += 1

    freq_animations = []

    for ind in range(len(vibs)):
        anim = f"{num_atoms}\nCalcUS\n"
        for ind2, (a, x, y, z) in enumerate(struct):
            anim += "{} {:.4f} {:.4f} {:.4f} {} {} {}\n".format(
                a, x, y, z, *vibs[ind][ind2]
            )
        freq_animations.append(anim)

    x = np.arange(500, 4000, 1)  # Wave number in cm^-1
    spectrum = plot_vibs(x, zip(wavenumbers, intensities))

    ir_spectrum = "Wavenumber,Intensity\n"

    intensities = 1000 * np.array(intensities) / max(intensities)
    for _x, i in sorted((zip(list(x), spectrum)), reverse=True):
        ir_spectrum += f"-{_x:.1f},{i:.5f}\n"

    parse_gaussian_charges(calc, calc.structure)
    prop.freq_list = wavenumbers
    prop.freq_animations = freq_animations
    prop.ir_spectrum = ir_spectrum
    prop.save()
    return ErrorCodes.SUCCESS


def gaussian_ts(in_file, calc):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))

    ret = launch_gaussian_calc(in_file, calc, ["calc.log"])

    if ret != ErrorCodes.SUCCESS:
        return ret

    with open(f"{local_folder}/calc.log") as f:
        lines = f.readlines()
        ind = len(lines) - 1

        try:
            while lines[ind].find("SCF Done") == -1:
                ind -= 1
            E = float(lines[ind].split()[4])
            while (
                lines[ind].find(
                    "Center     Atomic      Atomic             Coordinates (Angstroms)"
                )
                == -1
            ):
                ind += 1
            ind += 3
        except IndexError:
            logger.error(
                f"Could not parse the output of calculation {calc.id}: invalid format"
            )
            return ErrorCodes.INVALID_OUTPUT

        xyz = []
        while lines[ind].find("----") == -1:
            n, a, t, x, y, z = lines[ind].strip().split()
            xyz.append([ATOMIC_SYMBOL[int(a)], x, y, z])
            ind += 1

        xyz_structure = f"{len(xyz)}\nCalcUS\n"
        for el in xyz:
            xyz_structure += "{} {} {} {}\n".format(*el)

        xyz_structure = clean_xyz(xyz_structure)

    s = Structure.objects.get_or_create(
        parent_ensemble=calc.result_ensemble,
        xyz_structure=xyz_structure,
        number=calc.structure.number,
    )[0]
    s.degeneracy = calc.structure.degeneracy
    prop = get_or_create(calc.parameters, s)
    prop.energy = E
    prop.geom = True
    s.save()
    prop.save()

    parse_gaussian_charges(calc, s)
    return ErrorCodes.SUCCESS


def gaussian_scan(in_file, calc):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))

    ret = launch_gaussian_calc(in_file, calc, ["calc.log"])

    has_scan = "scan" in calc.constraints.lower()

    failed = False
    if ret != ErrorCodes.SUCCESS:
        if ret == ErrorCodes.SERVER_DISCONNECTED:
            return ret
        if has_scan:
            failed = True
        else:
            return ret

    with open(os.path.join(local_folder, "calc.log")) as f:
        lines = f.readlines()

    if has_scan:
        s_ind = 1
        ind = 0
        done = False
        while not done:
            try:
                while (
                    ind < len(lines) - 1
                    and lines[ind].find("Optimization completed.") == -1
                ):
                    ind += 1

                if ind == len(lines) - 1:
                    done = True
                    break

                ind2 = ind

                while (
                    lines[ind].find("Input orientation:") == -1
                    and lines[ind].find("Standard orientation:") == -1
                ):
                    ind += 1
                ind += 5
            except IndexError:
                logger.error(
                    f"Could not parse the output of calculation {calc.id}: invalid format"
                )
                return ErrorCodes.INVALID_OUTPUT

            xyz = []
            while lines[ind].find("----") == -1:
                n, a, t, x, y, z = lines[ind].strip().split()
                xyz.append([ATOMIC_SYMBOL[int(a)], x, y, z])
                ind += 1

            xyz_structure = f"{len(xyz)}\nCalcUS\n"
            for el in xyz:
                xyz_structure += "{} {} {} {}\n".format(*el)

            xyz_structure = clean_xyz(xyz_structure)

            try:
                while lines[ind2].find("SCF Done") == -1:
                    ind2 -= 1
            except IndexError:
                logger.error(
                    f"Could not parse the output of calculation {calc.id}: invalid format"
                )
                return ErrorCodes.INVALID_OUTPUT

            E = float(lines[ind2].split()[4])

            try:
                s = Structure.objects.get(
                    parent_ensemble=calc.result_ensemble, number=s_ind
                )
            except:
                s = Structure.objects.get_or_create(
                    parent_ensemble=calc.result_ensemble,
                    xyz_structure=xyz_structure,
                    number=s_ind,
                )[0]
            else:
                s.xyz_structure = xyz_structure

            s.degeneracy = 1

            prop = get_or_create(calc.parameters, s)
            prop.energy = E
            prop.geom = True
            s.save()
            prop.save()

            s_ind += 1
    else:
        ind = len(lines) - 1

        try:
            while lines[ind].find("SCF Done") == -1:
                ind -= 1
            E = float(lines[ind].split()[4])
            while (
                lines[ind].find(
                    "Center     Atomic      Atomic             Coordinates (Angstroms)"
                )
                == -1
            ):
                ind += 1
            ind += 3
        except IndexError:
            logger.error(
                f"Could not parse the output of calculation {calc.id}: invalid format"
            )
            return ErrorCodes.INVALID_OUTPUT

        xyz = []
        while lines[ind].find("----") == -1:
            n, a, t, x, y, z = lines[ind].strip().split()
            xyz.append([ATOMIC_SYMBOL[int(a)], x, y, z])
            ind += 1

        xyz_structure = f"{len(xyz)}\nCalcUS\n"
        for el in xyz:
            xyz_structure += "{} {} {} {}\n".format(*el)

        xyz_structure = clean_xyz(xyz_structure)

        s = Structure.objects.get_or_create(
            parent_ensemble=calc.result_ensemble,
            xyz_structure=xyz_structure,
            number=calc.structure.number,
        )[0]
        s.degeneracy = calc.structure.degeneracy
        prop = get_or_create(calc.parameters, s)
        prop.energy = E
        prop.geom = True
        s.save()
        prop.save()
    try:
        struct = calc.result_ensemble.structure_set.latest("id")
    except Structure.DoesNotExist:
        struct = False

    if struct:
        parse_gaussian_charges(calc, struct)

    if failed:
        return ret
    else:
        return ErrorCodes.SUCCESS


def gaussian_nmr(in_file, calc):
    local_folder = os.path.join(CALCUS_SCR_HOME, str(calc.id))

    ret = launch_gaussian_calc(in_file, calc, ["calc.log"])

    if ret != ErrorCodes.SUCCESS:
        return ret

    with open(os.path.join(local_folder, "calc.log")) as f:
        lines = f.readlines()

    try:
        ind = len(lines) - 1
        while lines[ind].find("SCF GIAO Magnetic shielding tensor (ppm):") == -1:
            ind -= 1

        nmr = ""
        ind += 1
        while lines[ind].find("End of Minotr") == -1:
            sline = lines[ind].strip().split()
            nmr += f"{int(sline[0])} {sline[1]} {sline[4]}\n"
            ind += 5

        while lines[ind].find("SCF Done") == -1:
            ind -= 1
        E = float(lines[ind].split()[4])
    except IndexError:
        logger.error(
            f"Could not parse the output of calculation {calc.id}: invalid format"
        )
        return ErrorCodes.INVALID_OUTPUT

    prop = get_or_create(calc.parameters, calc.structure)
    prop.simple_nmr = nmr
    prop.energy = E
    prop.save()

    parse_gaussian_charges(calc, calc.structure)
    return ErrorCodes.SUCCESS


def dist(a, b):
    return math.sqrt((a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2 + (a[3] - b[3]) ** 2)


COV_THRESHOLD = 1.1


def find_bonds(xyz):
    bonds = []

    def bond_unique(ind1, ind2):
        for bond in bonds:
            if bond[0] == ind1 and bond[1] == ind2:
                return False
            if bond[0] == ind2 and bond[1] == ind1:
                return False
        return True

    doubles = {
        "CC": 1.34,
        "CN": 1.29,
        "CO": 1.20,
        "CS": 1.60,
        "NC": 1.29,
        "OC": 1.20,
        "SC": 1.60,
        "NN": 1.25,
        "NO": 1.22,
        "ON": 1.22,
        "SO": 1.44,
        "OS": 1.44,
    }
    d_exist = list(doubles.keys())
    for ind1, i in enumerate(xyz):
        for ind2, j in enumerate(xyz):
            if ind1 > ind2:
                d = dist(i, j)
                btype = f"{i[0]}{j[0]}"
                cov = (
                    periodictable.elements[ATOMIC_NUMBER[i[0]]].covalent_radius
                    + periodictable.elements[ATOMIC_NUMBER[j[0]]].covalent_radius
                )
                if d_exist.count(btype):
                    factor = cov - doubles[btype]
                    b_order = ((cov - d) / factor) + 1
                    if b_order > 2.2:
                        bond_type = 3
                    elif b_order > 1.8:
                        bond_type = 2
                    elif b_order > 1.6:
                        bond_type = 4
                    else:
                        bond_type = 1
                else:
                    bond_type = 1
                corr_ratio = d / cov
                if corr_ratio < COV_THRESHOLD and bond_unique(ind1, ind2):
                    # btag = '%1s_%1s' % (self.atoms[i].label, self.atoms[j].label)
                    bonds.append([ind1, ind2, bond_type])
    return bonds


def write_mol(xyz):

    bonds = find_bonds(xyz)
    content = []
    content.append("Molfile\n")
    content.append("  CalcUS\n")
    content.append("empty\n")
    content.append(
        "%3d%3d%3d%3d%3d%3d%3d%3d%3d%6s V2000 \n"
        % (len(xyz), len(bonds), 0, 0, 0, 0, 0, 0, 0, "0999")
    )
    for atom in xyz:
        content.append(
            "%10.4f%10.4f%10.4f %-3s 0  0  0  0  0  0  0  0  0  0  0  0\n"
            % (atom[1], atom[2], atom[3], atom[0])
        )
    for bond in bonds:
        content.append("%3d%3d%3d  0  0  0  0\n" % (1 + bond[0], 1 + bond[1], bond[2]))
    content.append("M  END\n")
    return content


def write_xyz(xyz, path):
    with open(path, "w") as out:
        out.write(f"{len(xyz)}\n\n")
        for line in xyz:
            out.write("{} {:.4f} {:.4f} {:.4f}\n".format(line[0], *line[1]))


def gen_fingerprint(structure):
    if structure.xyz_structure == "":
        logger.error("No xyz structure!")
        return -1

    raw_xyz = structure.xyz_structure

    xyz = []
    for line in raw_xyz.split("\n")[2:]:
        if line.strip() != "":
            a, x, y, z = line.strip().split()
            xyz.append([a, float(x), float(y), float(z)])

    t = f"{time.time()}_{structure.id}"
    mol = write_mol(xyz)

    with open(f"/tmp/{t}.mol", "w") as out:
        for l in mol:
            out.write(l)

    with open("/dev/null", "w") as stream:
        out = subprocess.check_output(
            shlex.split(f"obabel /tmp/{t}.mol -oinchi -xX 'DoNotAddH'"),
            stderr=stream,
        ).decode("utf-8")

    inchi = out.split("\n")[-2]
    if inchi[:6] != "InChI=":
        logger.warning(f"Invalid InChI key obtained for structure {structure.id}")
        return ""
    else:
        return inchi[6:]


def analyse_opt(calc_id):
    funcs = {
        "Gaussian": analyse_opt_Gaussian,
        "ORCA": analyse_opt_ORCA,
        "xtb": analyse_opt_xtb,
    }

    calc = Calculation.objects.get(pk=calc_id)

    xyz = parse_xyz_from_text(calc.structure.xyz_structure)

    if len(xyz) == 1:  # Single atom
        return

    software = calc.parameters.software

    return funcs[software](calc)


def analyse_opt_ORCA(calc):
    prepath = os.path.join(CALCUS_SCR_HOME, str(calc.id))

    RMSDs = [0]

    if not os.path.isfile(os.path.join(prepath, "calc.out")):
        return

    if not os.path.isfile(os.path.join(prepath, "calc_trj.xyz")):
        return

    with open(os.path.join(prepath, "calc.out")) as f:
        lines = f.readlines()
    ind = 0
    flag = False
    while not flag:
        while lines[ind].find("RMS step") == -1:
            if lines[ind].find("THE OPTIMIZATION HAS CONVERGED") != -1:
                RMSDs.append(0.0)
            ind += 1
            if ind > len(lines) - 2:
                flag = True
                break
        if not flag:
            rms = float(lines[ind].split()[2])
            RMSDs.append(rms)
            ind += 1

    with open(os.path.join(prepath, "calc_trj.xyz")) as f:
        lines = f.readlines()

    num = int(lines[0])
    nstructs = int(len(lines) / (num + 2))

    new_frames = []
    for i in range(1, nstructs):
        xyz = "".join(lines[(num + 2) * i : (num + 2) * (i + 1)])
        try:
            f = calc.calculationframe_set.get(number=i)
        except CalculationFrame.DoesNotExist:
            new_frames.append(
                CalculationFrame(
                    number=i, xyz_structure=xyz, parent_calculation=calc, RMSD=RMSDs[i]
                )
            )
        else:
            continue
    CalculationFrame.objects.bulk_create(new_frames)


def analyse_opt_xtb(calc):
    if calc.step.name == "Minimum Energy Path":
        path = os.path.join(CALCUS_SCR_HOME, str(calc.id), "calc_MEP_trj.xyz")
    else:
        path = os.path.join(CALCUS_SCR_HOME, str(calc.id), "xtbopt.log")

    if not os.path.isfile(path):
        return

    with open(path) as f:
        lines = f.readlines()

    xyz = "".join(lines)
    natoms = int(lines[0])
    nn = int(len(lines) / (natoms + 2))

    to_update = []
    to_create = []
    if calc.step.name == "Minimum Energy Path":
        for n in range(nn):
            xyz = "".join(lines[(natoms + 2) * n : (natoms + 2) * (n + 1)])
            E = float(lines[n * (natoms + 2) + 1].split()[-1])
            try:
                f = calc.calculationframe_set.get(number=n + 1)
            except CalculationFrame.DoesNotExist:
                to_create.append(
                    CalculationFrame(
                        parent_calculation=calc,
                        number=n + 1,
                        RMSD=0,
                        xyz_structure=xyz,
                        energy=E,
                        converged=True,
                    )
                )
            else:
                f.xyz_structure = xyz
                f.energy = E
                to_update.append(f)

        CalculationFrame.objects.bulk_update(to_update, ["xyz_structure", "energy"])
        CalculationFrame.objects.bulk_create(to_create)
    else:
        for n in range(nn):
            xyz = "".join(lines[(natoms + 2) * n : (natoms + 2) * (n + 1)])
            rms = lines[n * (natoms + 2) + 1].split()[3]
            try:
                f = calc.calculationframe_set.get(number=n + 1)
            except CalculationFrame.DoesNotExist:
                to_create.append(
                    CalculationFrame(
                        parent_calculation=calc,
                        number=n + 1,
                        RMSD=rms,
                        xyz_structure=xyz,
                    )
                )
            else:
                continue
        CalculationFrame.objects.bulk_update(to_update, ["xyz_structure", "RMSD"])
        CalculationFrame.objects.bulk_create(to_create)


def analyse_opt_Gaussian(calc):
    calc_path = os.path.join(CALCUS_SCR_HOME, str(calc.id), "calc.log")

    if not os.path.isfile(calc_path):
        return

    _calc = Calculation.objects.prefetch_related("calculationframe_set").get(pk=calc.id)
    frames = _calc.calculationframe_set

    with open(calc_path, encoding="utf8", errors="ignore") as f:
        lines = f.readlines()

    if not calc.step.creates_ensemble:
        return

    orientation_str = "Input orientation"

    ind = 0
    s_ind = 0
    try:
        while lines[ind].find("Symbolic Z-matrix:") == -1:
            ind += 1
    except IndexError:
        logger.error(f"Could not parse Gaussian log for calc {calc.id}")
        return
    ind += 2

    start_ind = ind

    while lines[ind].strip() != "":
        ind += 1
    num_atoms = ind - start_ind

    xyz = ""

    E = 0
    to_update = []
    to_create = []
    while ind < len(lines) - 2:
        if lines[ind].find(orientation_str) != -1:
            s_ind += 1
            xyz += f"{num_atoms}\n\n"
            ind += 5
            while ind < len(lines) - 2 and lines[ind].find("---------") == -1:
                try:
                    n, z, T, X, Y, Z = lines[ind].strip().split()
                except ValueError:
                    return
                A = ATOMIC_SYMBOL[int(z)]
                xyz += f"{A} {X} {Y} {Z}\n"
                ind += 1
        elif lines[ind].find("SCF Done:") != -1:
            E = float(lines[ind].split()[4])
            ind += 1
        elif lines[ind].find("RMS     Displacement") != -1:
            rms = float(lines[ind].split()[2])
            if (
                lines[ind].split()[-1] == "YES"
                and lines[ind - 1].split()[-1] == "YES"
                and lines[ind - 2].split()[-1] == "YES"
                and lines[ind - 3].split()[-1] == "YES"
            ):
                converged = True
            else:
                converged = False

            assert E != 0

            try:
                f = frames.get(number=s_ind)
            except CalculationFrame.DoesNotExist:
                to_create.append(
                    CalculationFrame(
                        number=s_ind,
                        xyz_structure=xyz,
                        parent_calculation=calc,
                        RMSD=rms,
                        converged=converged,
                        energy=E,
                    )
                )
            else:
                # Really necessary? Not sure there is a good case where frames should systematically be overwritten
                f.xyz_structure = xyz
                f.energy = E
                to_update.append(f)
            xyz = ""
            ind += 1
        else:
            ind += 1
            if ind > len(lines) - 3:
                calc.save()
                CalculationFrame.objects.bulk_update(
                    to_update, ["xyz_structure", "energy"], batch_size=100
                )
                CalculationFrame.objects.bulk_create(to_create)
                return


def get_Gaussian_xyz(text):
    lines = text.split("\n")
    ind = len(lines) - 1
    while lines[ind].find("Coordinates (Angstroms)") == -1:
        ind -= 1

    ind += 3
    s = []
    while lines[ind].find("----------") == -1:
        if lines[ind].strip() != "":
            _, n, _, x, y, z = lines[ind].split()
            s.append((ATOMIC_SYMBOL[int(n)], x, y, z))
        ind += 1
    xyz = f"{len(s)}\n\n"
    for l in s:
        xyz += "{} {} {} {}\n".format(*l)
    return clean_xyz(xyz)


SPECIAL_FUNCTIONALS = ["HF-3c", "PBEh-3c"]
BASICSTEP_TABLE = {
    "xtb": {
        "Geometrical Optimisation": xtb_opt,
        "Conformational Search": crest,
        "Constrained Optimisation": xtb_scan,
        "Frequency Calculation": xtb_freq,
        "TS Optimisation": xtb_ts,
        "UV-Vis Calculation": xtb_stda,
        "Single-Point Energy": xtb_sp,
        "Minimum Energy Path": xtb_mep,
        "Constrained Conformational Search": crest,
    },
    "ORCA": {
        "NMR Prediction": orca_nmr,
        "Geometrical Optimisation": orca_opt,
        "TS Optimisation": orca_ts,
        "MO Calculation": orca_mo_gen,
        "Frequency Calculation": orca_freq,
        "Constrained Optimisation": orca_scan,
        "Single-Point Energy": orca_sp,
    },
    "Gaussian": {
        "NMR Prediction": gaussian_nmr,
        "Geometrical Optimisation": gaussian_opt,
        "TS Optimisation": gaussian_ts,
        "Frequency Calculation": gaussian_freq,
        "Constrained Optimisation": gaussian_scan,
        "Single-Point Energy": gaussian_sp,
        "UV-Vis Calculation": gaussian_td,
    },
}

time_dict = {}


def filter(order, input_structures):
    if order.filter == None:
        return input_structures

    structures = []

    if order.filter.type == "By Number":
        allowed_nums = [int(i) for i in order.filter.value.split(",")]
        for s in input_structures:
            if s.number in allowed_nums:
                structures.append(s)
        return structures

    summary, hashes = input_structures[0].parent_ensemble.ensemble_summary

    target_hash = order.filter.parameters.md5

    if target_hash not in summary.keys():
        return []

    summary_data = summary[target_hash]

    def get_weight(s):
        ind = summary_data[0].index(s.number)
        return summary_data[6][ind]

    def get_rel_energy(s):
        ind = summary_data[0].index(s.number)
        return summary_data[5][ind]

    if order.filter.type == "By Boltzmann Weight":
        for s in input_structures:
            if get_weight(s) > float(order.filter.value):
                structures.append(s)
    elif order.filter.type == "By Relative Energy":
        for s in input_structures:
            val = get_rel_energy(s)
            if order.author.pref_units == 0:
                E = val * HARTREE_FVAL
            elif order.author.pref_units == 1:
                E = val * HARTREE_TO_KCAL_F
            elif order.author.pref_units == 2:
                E = val
            if E < float(order.filter.value):
                structures.append(s)

    return structures


@app.task(base=AbortableTask)
def dispatcher_flowchart():
    print("Just for testing tasks")


@app.task(base=AbortableTask)
def dispatcher(order_id, drawing=None, is_flowchart=False, flowchartStepObjectId=None):
    stepFlowchart = None
    should_create_ensemble = True
    if is_flowchart:
        order = FlowchartOrder.objects.get(pk=order_id)
        flowchartStepObject = Step.objects.get(pk=flowchartStepObjectId)
        if flowchartStepObject.parentId.step is not None:
            if flowchartStepObject.parentId.step.creates_ensemble is True:
                if len(flowchartStepObject.calculation_set.all()) != 0:
                    step_ensemble = flowchartStepObject.calculation_set.all()[
                        0
                    ].result_ensemble
                    should_create_ensemble = False
        stepFlowchart = flowchartStepObject.step
    else:
        order = CalculationOrder.objects.get(pk=order_id)
    if should_create_ensemble is True:
        ensemble = order.ensemble
    else:
        ensemble = step_ensemble

    local = True
    if is_flowchart is not True:
        if order.resource is not None:
            local = False

    if stepFlowchart is None:
        step = order.step
    else:
        step = stepFlowchart

    mode = "e"  # Mode for input structure (Ensemble/Structure)
    input_structures = None
    if order.structure != None:
        mode = "s"
        molecule = order.structure.parent_ensemble.parent_molecule
        if order.project == molecule.project:
            ensemble = order.structure.parent_ensemble
            input_structures = [order.structure]
        else:
            molecule = Molecule.objects.create(
                name=molecule.name, inchi=molecule.inchi, project=order.project
            )
            if is_flowchart is True and should_create_ensemble is False:
                ensemble = step_ensemble
            else:
                ensemble = Ensemble.objects.create(
                    name=order.structure.parent_ensemble.name, parent_molecule=molecule
                )
            structure = Structure.objects.get_or_create(
                parent_ensemble=ensemble,
                xyz_structure=order.structure.xyz_structure,
                number=1,
            )[0]
            order.structure = structure
            molecule.save()
            if should_create_ensemble is True:
                ensemble.save()
            structure.save()
            order.save()
            input_structures = [structure]
    elif order.ensemble != None:
        if ensemble.parent_molecule is None:
            raise Exception(f"Ensemble {ensemble.id} has no parent molecule")
        elif ensemble.parent_molecule.inchi == "":
            fingerprint = ""
            for s in ensemble.structure_set.all():
                fing = gen_fingerprint(s)
                if fingerprint == "":
                    fingerprint = fing
                else:
                    if fingerprint != fing:  #####
                        pass
            try:
                molecule = Molecule.objects.get(
                    inchi=fingerprint, project=order.project
                )
            except Molecule.DoesNotExist:
                ensemble.parent_molecule.inchi = fingerprint
                ensemble.parent_molecule.save()
                molecule = ensemble.parent_molecule
            else:
                _mol = ensemble.parent_molecule
                ensemble.parent_molecule = molecule
                ensemble.save()
                _mol.delete()

            input_structures = ensemble.structure_set.all()
        else:
            if ensemble.parent_molecule.project == order.project:
                molecule = ensemble.parent_molecule
                input_structures = ensemble.structure_set.all()
            else:
                molecule = Molecule.objects.create(
                    name=ensemble.parent_molecule.name,
                    inchi=ensemble.parent_molecule.inchi,
                    project=order.project,
                )
                if is_flowchart is True and should_create_ensemble is False:
                    ensemble = step_ensemble
                else:
                    ensemble = Ensemble.objects.create(
                        name=ensemble.name, parent_molecule=molecule
                    )
                for s in order.ensemble.structure_set.all():
                    _s = Structure.objects.get_or_create(
                        parent_ensemble=ensemble,
                        xyz_structure=s.xyz_structure,
                        number=s.number,
                    )[0]
                    _s.degeneracy = s.degeneracy
                    _s.save()
                order.ensemble = ensemble
                order.save()
                if should_create_ensemble is True:
                    ensemble.save()
                molecule.save()
                input_structures = ensemble.structure_set.all()
    elif order.start_calc != None:
        calc = order.start_calc
        fid = order.start_calc_frame
        mode = "c"

        molecule = calc.result_ensemble.parent_molecule
        if is_flowchart is True and should_create_ensemble is False:
            ensemble = step_ensemble
        else:
            ensemble = Ensemble.objects.create(
                parent_molecule=molecule,
                origin=calc.result_ensemble,
                name=f"Extracted frame {fid}",
            )
        f = calc.calculationframe_set.get(number=fid)
        s = Structure.objects.get_or_create(
            parent_ensemble=ensemble,
            xyz_structure=f.xyz_structure,
            number=order.start_calc.structure.number,
        )[0]
        s.degeneracy = 1
        prop = Property.objects.create(
            parent_structure=s, parameters=calc.parameters, geom=True
        )
        prop.save()
        if should_create_ensemble is True:
            ensemble.save()
        s.save()
        input_structures = [s]
    else:
        logger.error(f"Invalid calculation order: {order.id}")
        return

    group_order = []
    calculations = []

    input_structures = filter(order, input_structures)
    if step.creates_ensemble:
        if order.name.strip() == "":
            e = Ensemble.objects.create(
                name=f"{order.step.name} Result",
                origin=ensemble,
                parent_molecule=molecule,
            )
        else:
            e = Ensemble.objects.create(
                name=order.name, origin=ensemble, parent_molecule=molecule
            )

        order.result_ensemble = e
        order.save()

        for s in input_structures:
            if is_flowchart is not True:
                c = Calculation.objects.create(
                    structure=s,
                    order=order,
                    date_submitted=timezone.now(),
                    step=step,
                    parameters=order.parameters,
                    result_ensemble=e,
                    constraints=order.constraints,
                    aux_structure=order.aux_structure,
                )
            else:
                c = Calculation.objects.create(
                    structure=s,
                    flowchart_order=order,
                    date_submitted=timezone.now(),
                    step=step,
                    parameters=flowchartStepObject.parameters,
                    result_ensemble=e,
                    flowchart_step=flowchartStepObject,
                )
            c.save()
            if local:
                calculations.append(c)
                if not is_test:
                    group_order.append(run_calc.s(c.id).set(queue="comp"))
                else:
                    group_order.append(run_calc.s(c.id))
            else:
                calculations.append(c)
                c.local = False
                c.save()
                cmd = f"launch\n{c.id}\n"
                send_cluster_command(cmd)

    else:
        if mode == "c":
            order.result_ensemble = ensemble
            order.save()
        for s in input_structures:
            if is_flowchart is not True:
                c = Calculation.objects.create(
                    structure=s,
                    order=order,
                    date_submitted=timezone.now(),
                    parameters=order.parameters,
                    step=step,
                    constraints=order.constraints,
                    aux_structure=order.aux_structure,
                )
            else:
                c = Calculation.objects.create(
                    structure=s,
                    flowchart_order=order,
                    date_submitted=timezone.now(),
                    parameters=flowchartStepObject.parameters,
                    step=step,
                    flowchart_step=flowchartStepObject,
                )
            c.save()
            if local:
                calculations.append(c)
                if not is_test:
                    group_order.append(run_calc.s(c.id).set(queue="comp"))
                else:
                    group_order.append(run_calc.s(c.id))
            else:
                c.local = False
                c.save()

                cmd = f"launch\n{c.id}\n"
                send_cluster_command(cmd)

    for task, c in zip(group_order, calculations):
        res = task.apply_async()
        c.task_id = res
        c.save()


def add_input_to_calc(calc):
    inp = calc_to_ccinput(calc)
    if isinstance(inp, CCInputException):
        msg = f"CCInput error: {str(inp)}"
        if is_test:
            print(msg)
        calc.error_message = msg
        calc.status = 3
        calc.save()
        return ErrorCodes.FAILED_TO_CREATE_INPUT

    calc.input_file = inp.input_file
    calc.parameters.specifications = inp.confirmed_specifications

    if hasattr(inp, "command"):
        calc.command = inp.command

    calc.save()
    calc.parameters.save()


@app.task(base=AbortableTask)
def run_calc(calc_id):
    if not is_test:
        logger.info(f"Processing calc {calc_id}")

    def get_calc(calc_id):
        for i in range(5):
            try:
                calc = Calculation.objects.get(pk=calc_id)
            except Calculation.DoesNotExist:
                time.sleep(1)
            else:
                if not is_test and calc.task_id == "" and calc.local:
                    time.sleep(1)
                else:
                    return calc
        raise Exception("Could not get calculation to run")

    try:
        calc = get_calc(calc_id)
    except Exception:
        logger.info(f"Could not get calculation {calc_id}")
        return ErrorCodes.UNKNOWN_TERMINATION

    f = BASICSTEP_TABLE[calc.parameters.software][calc.step.name]

    workdir = os.path.join(CALCUS_SCR_HOME, str(calc.id))

    if calc.status == 3:  # Already revoked:
        logger.info(f"Calc {calc_id} already revoked")
        return ErrorCodes.JOB_CANCELLED

    if calc.parameters.software == "xtb" and calc.step.short_name in ["mep", "optts"]:
        calc.parameters.software = "ORCA"
        ret = add_input_to_calc(calc)
        calc.parameters.software = "xtb"
    else:
        ret = add_input_to_calc(calc)

    if isinstance(ret, ErrorCodes):
        return ret

    in_file = os.path.join(workdir, "in.xyz")

    if calc.status == 0:
        os.makedirs(workdir, exist_ok=True)

        with open(in_file, "w") as out:
            out.write(clean_xyz(calc.structure.xyz_structure))

    if not calc.local and calc.remote_id == 0:
        logger.debug(f"Preparing remote folder for calc {calc_id}")
        pid = int(threading.get_ident())
        conn = connections[pid]
        lock = locks[pid]
        remote_dir = remote_dirs[pid]

        if calc.status == 0:
            direct_command(f"mkdir -p {remote_dir}", conn, lock)
            sftp_put(in_file, os.path.join(remote_dir, "in.xyz"), conn, lock)

        in_file = os.path.join(remote_dir, "in.xyz")

    logger.info(f"Running calc {calc_id}")
    try:
        ret = f(in_file, calc)
    except Exception as e:
        ret = ErrorCodes.UNKNOWN_TERMINATION
        traceback.print_exc()

        calc.refresh_from_db()
        calc.date_finished = timezone.now()

        calc.status = 3
        calc.error_message = f"Incorrect termination ({str(e)})"
        calc.save()
        logger.info(f"Error while running calc {calc_id}: '{str(e)}'")
    else:
        calc.refresh_from_db()
        calc.date_finished = timezone.now()

        if ret == ErrorCodes.JOB_CANCELLED:
            pid = int(threading.get_ident())
            if pid in kill_sig:
                kill_sig.remove(pid)
            calc.status = 3
            calc.error_message = "Job cancelled"
            logger.info(f"Job {calc.id} cancelled")
        elif ret == ErrorCodes.SERVER_DISCONNECTED:
            return ret
        elif ret == ErrorCodes.SUCCESS:
            calc.status = 2
        elif ret == ErrorCodes.FAILED_TO_RUN_LOCAL_SOFTWARE:
            calc.status = 3
            if calc.error_message == "":
                calc.error_message = "Failed to execute the relevant command"
        else:
            logger.warning(f"Calculation {calc.id} finished with return code {ret}")
            calc.status = 3
            calc.error_message = "Unknown termination"

    logger.info(f"Calc {calc_id} finished")

    if calc.step.creates_ensemble:
        analyse_opt(calc.id)

    output_files = {}
    for log_name in glob.glob(
        os.path.join(CALCUS_SCR_HOME, str(calc.id), "*.log")
    ) + glob.glob(os.path.join(CALCUS_SCR_HOME, str(calc.id), "*.out")):
        fname = os.path.basename(log_name)[:-4]
        with open(log_name) as f:
            log = "".join(f.readlines())
        output_files[fname] = log

    calc.output_files = json.dumps(output_files)
    calc.save()

    global cache_ind

    if (
        is_test
        and os.getenv("CAN_USE_CACHED_LOGS") == "true"
        and os.getenv("USE_CACHED_LOGS") == "true"
        and not calc_is_cached(calc)
    ):
        # Get the test name for convenient labeling
        # If not available, the code is running in a thread of the cluster daemon
        test_name = os.environ.get(
            "TEST_NAME", f"frontend.test_cluster.unknown_{time.time()}"
        )
        if cache_ind != 1:
            # If there are multiple calculations per test, they need to be named differently
            test_name += f"_{cache_ind}"

        logger.info(f"Adding calculation results of {test_name} to the cache")
        shutil.copytree(
            f"{CALCUS_SCR_HOME}/{calc.id}",
            os.path.join(CALCUS_CACHE_HOME, test_name),
            dirs_exist_ok=True,
        )
        with open(os.path.join(CALCUS_CACHE_HOME, test_name + ".input"), "w") as out:
            out.write(calc.all_inputs)

    cache_ind += 1

    return ret


@app.task
def del_order(order_id):
    _del_order(order_id)


@app.task
def del_project(proj_id):
    _del_project(proj_id)


@app.task
def del_molecule(mol_id):
    _del_molecule(mol_id)


@app.task
def del_ensemble(ensemble_id):
    _del_ensemble(ensemble_id)


def _del_order(id):
    o = CalculationOrder.objects.get(pk=id)
    for c in o.calculation_set.all():
        _del_calculation(c)

    if o.result_ensemble:
        _del_ensemble(o.result_ensemble.id)

    # The order is automatically deleted with the last calculation
    # If it has no calculation because of a bug:
    try:
        o.refresh_from_db()
    except CalculationOrder.DoesNotExist:
        pass
    else:
        if o.pk:
            o.delete()


def _del_project(id):
    proj = Project.objects.get(pk=id)
    proj.author = None
    proj.save()
    for m in proj.molecule_set.all():
        _del_molecule(m.id)
    proj.delete()


def _del_molecule(id):
    mol = Molecule.objects.get(pk=id)
    for e in mol.ensemble_set.all():
        _del_ensemble(e.id)
    mol.delete()


def _del_calculation(calc):
    if calc.status == 1 or calc.status == 2:
        kill_calc(calc)

    calc.delete()
    try:
        rmtree(os.path.join(CALCUS_SCR_HOME, str(calc.id)))
    except OSError:
        pass


def _del_ensemble(id):
    try:
        e = Ensemble.objects.get(pk=id)
    except Ensemble.DoesNotExist:
        return

    for s in e.structure_set.all():
        _del_structure(s)

    for c in e.calculation_set.all():
        _del_calculation(c)

    e.delete()


def _del_structure(s):
    calcs = s.calculation_set.all()
    for c in calcs:
        _del_calculation(c)

    s.delete()


def send_cluster_command(cmd):
    connection = redis.Redis(host="redis", port=6379, db=2)
    _cmd = cmd.replace("\n", "&")
    connection.rpush("cluster", _cmd)
    connection.close()


@app.task
def cancel(calc_id):
    logger.info(f"Cancelling calc {calc_id}")
    calc = Calculation.objects.get(pk=calc_id)
    kill_calc(calc)


def kill_calc(calc):
    if calc.local:
        if calc.task_id != "":
            if calc.status == 1:
                res = AbortableAsyncResult(calc.task_id)
                res.abort()
                calc.status = 3
                calc.error_message = "Job cancelled"
                calc.save()
            elif calc.status == 2:
                logger.warning("Cannot cancel calculation which is already done")
                return
            else:
                app.control.revoke(calc.task_id)
                calc.status = 3
                calc.error_message = "Job cancelled"
                calc.save()
        else:
            logger.error("Cannot cancel calculation without task id")
    else:
        cmd = f"kill\n{calc.id}\n"
        send_cluster_command(cmd)


@app.task
def backup_db():
    logger.info("Backup up database")
    management.call_command("dbbackup", clean=True, interactive=False)


@app.task
def ping_satellite():
    r = requests.post(
        "https://calcus-satellite-tg3y3xrnxq-uc.a.run.app/ping",
        data={"code": settings.PING_CODE},
    )
