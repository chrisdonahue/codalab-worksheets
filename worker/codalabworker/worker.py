from contextlib import closing
from subprocess import check_output
import logging
import multiprocessing
import os
import shutil
import threading
import time
import traceback
import re
import json

from bundle_service_client import BundleServiceException
from dependency_manager import DependencyManager
from worker_state_manager import WorkerStateManager
from file_util import remove_path, un_tar_directory
from run import Run
from docker_image_manager import DockerImageManager

VERSION = 18

logger = logging.getLogger(__name__)

"""
Resumable Workers

    If the worker process of a worker machine terminates and restarts while a
    bundle is running, the worker process is able to keep track of the running
    bundle once again, as long as the state is intact and the bundle container
    is still running or has finished running.
"""

class Worker(object):
    """
    This class is responsible for:

        1) Registering with the bundle service and receiving all messages
           sent to the worker.
        2) Managing all the runs currently executing on the worker and
           forwarding messages associated with those runs to the appropriate
           instance of the Run class.
        3) Spawning classes and threads that manage other worker resources,
           specifically the storage of bundles (both running bundles as well as
           their dependencies) and the cache of Docker images.
        4) Upgrading the worker.
    """

    def __init__(self, id, tag, work_dir, cpuset, gpuset,
                 max_work_dir_size_bytes, max_dependencies_serialized_length, max_images_bytes,
                 shared_file_system, bundle_service, docker, docker_network_prefix='codalab_worker_network'):
        self.id = id
        self._tag = tag
        self.shared_file_system = shared_file_system
        self._bundle_service = bundle_service
        self._docker = docker
        self._docker_network_prefix = docker_network_prefix

        self._resource_lock = threading.Lock() # lock for cpuset and gpuset
        self._cpuset = cpuset
        self._gpuset = gpuset
        self._cpuset_free = set(self._cpuset) # make a copy of self._cpuset as initial value
        self._gpuset_free = set(self._gpuset) # make a copy of self._gpuset as initial value

        self._worker_state_manager = WorkerStateManager(work_dir, self.shared_file_system)

        if not self.shared_file_system:
            # Manages which dependencies are available.
            self._dependency_manager = DependencyManager(
                    work_dir, max_work_dir_size_bytes, max_dependencies_serialized_length,
                    self._worker_state_manager.previous_runs.keys())
        self._image_manager = DockerImageManager(self._docker, work_dir, max_images_bytes)
        self._max_images_bytes = max_images_bytes

        self._exiting_lock = threading.Lock()
        self._exiting = False
        self._should_upgrade = False
        self._last_checkin_successful = False

        # set up docker networks for running bundles: one with external network access and one without
        self.docker_network_external_name = self._docker_network_prefix + "_ext"
        if self.docker_network_external_name not in self._docker.list_networks():
            logger.debug('Creating docker network: {}'.format(self.docker_network_external_name))
            self._docker.create_network(self.docker_network_external_name, internal=False)
        else:
            logger.debug('Docker network already exists, not creating: {}'.format(self.docker_network_external_name))

        self.docker_network_internal_name = self._docker_network_prefix + "_int"
        if self.docker_network_internal_name not in self._docker.list_networks():
            logger.debug('Creating docker network: {}'.format(self.docker_network_internal_name))
            self._docker.create_network(self.docker_network_internal_name)
        else:
            logger.debug('Docker network already exists, not creating: {}'.format(self.docker_network_internal_name))

    def run(self):
        if self._max_images_bytes is not None:
            self._image_manager.start_cleanup_thread()
        if not self.shared_file_system:
            self._dependency_manager.start_cleanup_thread()

        resumed_prev_runs = False
        def resume_previous_runs():
            # resume previous runs
            self._worker_state_manager.resume_previous_runs(
                    lambda run_info: Run.deserialize(
                        self._bundle_service, self._docker, self._image_manager, self, run_info)
            )

            # for each resumed run, remove the assigned cpu and gpus from the free sets
            with self._resource_lock:
                run_sets = self._worker_state_manager.map_runs(lambda run: (run._cpuset, run._gpuset))
                for cpuset, gpuset in run_sets:
                    for k in cpuset:
                        if k in self._cpuset:
                            self._cpuset_free.remove(k)
                        else:
                            logger.debug('Warning: cpu {} not in worker cpuset'.format(k))

                    for k in gpuset:
                        if k in self._gpuset:
                            self._gpuset_free.remove(k)
                        else:
                            logger.debug('Warning: gpu {} not in worker gpuset'.format(k))

            self._worker_state_manager.save_state()

        while self._should_run():
            try:
                self._checkin()

                # resume previous runs once in the beginning, but after checkin
                # this is not an ideal design because initial checkin can assign worker with new runs
                # but resources from previous runs are not re-allocated to the worker yet; this can cause
                # performance problems
                if not resumed_prev_runs:
                    resume_previous_runs()
                    resumed_prev_runs = True
                self._worker_state_manager.save_state()
                if not self._last_checkin_successful:
                    logger.info('Connected! Successful check in.')
                self._last_checkin_successful = True

            except Exception:
                self._last_checkin_successful = False
                traceback.print_exc()
                time.sleep(1)

        self._checkout()
        self._worker_state_manager.save_state()

        if self._max_images_bytes is not None:
            self._image_manager.stop_cleanup_thread()
        if not self.shared_file_system:
            self._dependency_manager.stop_cleanup_thread()

        if self._should_upgrade:
            self._upgrade()

    def signal(self):
        logger.info('Exiting: Will wait for exiting jobs to finish, but will not start any new jobs.')
        with self._exiting_lock:
            self._exiting = True

    def _is_exiting(self):
        with self._exiting_lock:
            return self._exiting

    def _should_run(self):
        if not self._is_exiting():
            return True
        return self._worker_state_manager.has_runs()

    def _get_installed_memory_bytes(self):
        try:
            return os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
        except ValueError:
            # Fallback to sysctl when os.sysconf('SC_PHYS_PAGES') fails on OS X
            return int(check_output(['sysctl', '-n', 'hw.memsize']).strip())

    def _get_allocated_memory_bytes(self):
        return sum(self._worker_state_manager.map_runs(lambda run: run.requested_memory_bytes))

    def _get_memory_bytes(self):
        return max(0, self._get_installed_memory_bytes() - self._get_allocated_memory_bytes())

    def _get_gpu_count(self):
        info = self._docker.get_nvidia_devices_info()
        count = 0 if info is None else len(info['Devices'])
        return count

    def _checkin(self):
        request = {
            'version': VERSION,
            'will_upgrade': self._should_upgrade,
            'tag': self._tag,
            'cpus': len(self._cpuset),
            'gpus': len(self._gpuset),
            'memory_bytes': self._get_memory_bytes(),
            'dependencies': [] if self.shared_file_system else self._dependency_manager.dependencies()
        }
        response = self._bundle_service.checkin(self.id, request)
        if response:
            type = response['type']
            logger.debug('Received %s message: %s', type, response)
            if type == 'run':
                self._run(response['bundle'], response['resources'])
            elif type == 'read':
                self._read(response['socket_id'], response['uuid'], response['path'],
                           response['read_args'])
            elif type == 'netcat':
                self._netcat(response['socket_id'], response['uuid'], response['port'],
                           response['message'])
            elif type == 'write':
                self._write(response['uuid'], response['subpath'],
                            response['string'])
            elif type == 'kill':
                self._kill(response['uuid'])
            elif type == 'upgrade':
                with self._exiting_lock:
                    self._exiting = True
                self._should_upgrade = True

    def _allocate_cpu_and_gpu_sets(self, request_cpus, request_gpus):
        """
        Allocate a cpuset and gpuset to assign to a bundle based on given requested resources.
        Side effects: updates the free sets, self._cpuset_free and self._gpuset_free

        Arguments:
            request_cpus: integer
            request_gpus: integer

        Returns a 2-tuple:
            cpuset: Allocated cpuset. Empty set if allocation was unsuccessful
            gpuset: Allocated gpuset. Empty set if allocation was unsuccessful
        """
        cpuset, gpuset = set(), set()

        with self._resource_lock:
            if len(self._cpuset_free) < request_cpus or len(self._gpuset_free) < request_gpus:
                return cpuset, gpuset

            for i in range(request_cpus):
                cpuset.add(self._cpuset_free.pop())
            for j in range(request_gpus):
                gpuset.add(self._gpuset_free.pop())
            return cpuset, gpuset

    def _deallocate_cpu_and_sets(self, cpuset, gpuset):
        """
        Release held up cpus and gpus

        Re-add cpuset and gpuset back to their respective free sets
        """
        with self._resource_lock:
            self._cpuset_free |= cpuset
            self._gpuset_free |= gpuset

    def _run(self, bundle, resources):
        if self.shared_file_system:
            bundle_path = bundle['location']
        else:
            bundle_path = self._dependency_manager.get_run_path(bundle['uuid'])

        cpuset, gpuset = self._allocate_cpu_and_gpu_sets(
                resources['request_cpus'], resources['request_gpus'])

        if len(cpuset) == 0 and len(gpuset) == 0: # revert self._cpuset_free and self._gpuset_free in-place
            logger.debug('Unsuccessful allocation of cpu and gpu sets for bundle %s', bundle['uuid'])
            return

        run = Run(self._bundle_service, self._docker, self._image_manager, self,
                  bundle, bundle_path, resources, cpuset, gpuset)
        if run.run():
            self._worker_state_manager.add_run(bundle['uuid'], run)
        else: # revert self._cpuset_free and self._gpuset_free in-place
            self._deallocate_cpu_and_sets(cpuset, gpuset)

    def add_dependency(self, parent_uuid, parent_path, uuid, loop_callback):
        """
        Registers that the run with UUID uuid depends on path parent_path in
        bundle with UUID parent_uuid. Downloads the dependency if necessary, and
        returns the path to the dependency. Note, remove_dependency should be
        called for every dependency added.

        loop_callback is a method that is called repeatedly while downloading
        the dependency. If that method throws an exception, the download gets
        interrupted and add_dependency fails with that same exception.
        """
        assert(not self.shared_file_system)
        dependency_path, should_download = (
            self._dependency_manager.add_dependency(parent_uuid, parent_path, uuid))
        if should_download:
            logger.debug('Downloading dependency %s/%s', parent_uuid, parent_path)
            try:
                download_success = False
                fileobj, target_type = (
                    self._bundle_service.get_bundle_contents(parent_uuid, parent_path))
                with closing(fileobj):
                    # "Bug" the fileobj's read function so that we can keep
                    # track of the number of bytes downloaded so far.
                    old_read_method = fileobj.read
                    bytes_downloaded = [0]
                    def interruptable_read(*args, **kwargs):
                        data = old_read_method(*args, **kwargs)
                        bytes_downloaded[0] += len(data)
                        loop_callback(bytes_downloaded[0])
                        return data
                    fileobj.read = interruptable_read

                    self._store_dependency(dependency_path, fileobj, target_type)
                    download_success = True
            finally:
                logger.debug('Finished downloading dependency %s/%s', parent_uuid, parent_path)
                self._dependency_manager.finish_download(
                    parent_uuid, parent_path, download_success)

        return dependency_path

    def _store_dependency(self, dependency_path, fileobj, target_type):
        try:
            if target_type == 'directory':
                un_tar_directory(fileobj, dependency_path, 'gz')
            else:
                with open(dependency_path, 'wb') as f:
                    shutil.copyfileobj(fileobj, f)
        except:
            remove_path(dependency_path)
            raise

    def remove_dependency(self, parent_uuid, parent_path, uuid):
        """
        Unregisters that the run with UUID uuid depends on path parent_path in
        bundle with UUID parent_uuid. This method is safe to call on
        dependencies that were never added with add_dependency.
        """
        assert(not self.shared_file_system)
        self._dependency_manager.remove_dependency(parent_uuid, parent_path, uuid)

    def _read(self, socket_id, uuid, path, read_args):
        run = self._worker_state_manager._get_run(uuid)
        if run is None:
            Run.read_run_missing(self._bundle_service, self, socket_id)
        else:
            # Reads may take a long time, so do the read in a separate thread.
            threading.Thread(target=Run.read,
                             args=(run, socket_id, path, read_args)).start()

    def _netcat(self, socket_id, uuid, port, message):
        run = self._worker_state_manager._get_run(uuid)
        if run is None:
            Run.read_run_missing(self._bundle_service, self, socket_id)
        else:
            # Reads may take a long time, so do the read in a separate thread.
            threading.Thread(target=Run.netcat,
                             args=(run, socket_id, port, message)).start()

    def _write(self, uuid, subpath, string):
        run = self._worker_state_manager._get_run(uuid)
        if run is not None:
            run.write(subpath, string)

    def _kill(self, uuid):
        run = self._worker_state_manager._get_run(uuid)
        if run is not None:
            run.kill('Kill requested')

    def finish_run(self, uuid):
        """
        Registers that the run with the given UUID has finished.
        """
        self._worker_state_manager.finish_run(uuid)
        if not self.shared_file_system:
            self._dependency_manager.finish_run(uuid)

    def _checkout(self):
        try:
            self._bundle_service.checkout(self.id)
        except BundleServiceException:
            traceback.print_exc()

    def _upgrade(self):
        logger.debug('Upgrading')
        worker_dir = os.path.dirname(os.path.realpath(__file__))

        while True:
            try:
                with closing(self._bundle_service.get_code()) as code:
                    remove_path(worker_dir)
                    un_tar_directory(code, worker_dir, 'gz')
                    break
            except Exception:
                traceback.print_exc()
                time.sleep(1)

        exit(123)