# Copyright (c) 2010 Cloud.com, Inc
# Copyright 2012 Cloudbase Solutions Srl
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

"""
Management class for basic VM operations.
"""
import functools
import os
import time

from eventlet import timeout as etimeout
from os_win import constants as os_win_const
from os_win import exceptions as os_win_exc
from os_win import utilsfactory
from oslo_concurrency import processutils
from oslo_log import log as logging
from oslo_service import loopingcall
from oslo_utils import excutils
from oslo_utils import fileutils
from oslo_utils import importutils
from oslo_utils import units
from oslo_utils import uuidutils

from nova.api.metadata import base as instance_metadata
from nova.compute import vm_states
import nova.conf
from nova import exception
from nova.i18n import _, _LI, _LE, _LW
from nova import utils
from nova.virt import configdrive
from nova.virt import hardware
from nova.virt.hyperv import constants
from nova.virt.hyperv import imagecache
from nova.virt.hyperv import pathutils
from nova.virt.hyperv import serialconsoleops
from nova.virt.hyperv import volumeops

LOG = logging.getLogger(__name__)


CONF = nova.conf.CONF

SHUTDOWN_TIME_INCREMENT = 5
REBOOT_TYPE_SOFT = 'SOFT'
REBOOT_TYPE_HARD = 'HARD'

VM_GENERATIONS = {
    constants.IMAGE_PROP_VM_GEN_1: constants.VM_GEN_1,
    constants.IMAGE_PROP_VM_GEN_2: constants.VM_GEN_2
}

VM_GENERATIONS_CONTROLLER_TYPES = {
    constants.VM_GEN_1: constants.CTRL_TYPE_IDE,
    constants.VM_GEN_2: constants.CTRL_TYPE_SCSI
}


def check_admin_permissions(function):
    @functools.wraps(function)
    def wrapper(self, *args, **kwds):

        # Make sure the windows account has the required admin permissions.
        self._vmutils.check_admin_permissions()
        return function(self, *args, **kwds)
    return wrapper

NEUTRON_VIF_DRIVER = 'nova.virt.hyperv.vif.HyperVNeutronVIFDriver'
NOVA_VIF_DRIVER = 'nova.virt.hyperv.vif.HyperVNovaNetworkVIFDriver'


def get_network_driver():
    """"Return the correct network module"""
    if nova.network.is_neutron() is None:
        # this is an unknown network type, not neutron or nova
        raise KeyError()
    elif nova.network.is_neutron():
        return NEUTRON_VIF_DRIVER
    else:
        return NOVA_VIF_DRIVER


class VMOps(object):
    # The console log is stored in two files, each should have at most half of
    # the maximum console log size.
    _MAX_CONSOLE_LOG_FILE_SIZE = units.Mi / 2
    _ROOT_DISK_CTRL_ADDR = 0

    def __init__(self):
        self._vmutils = utilsfactory.get_vmutils()
        self._metricsutils = utilsfactory.get_metricsutils()
        self._vhdutils = utilsfactory.get_vhdutils()
        self._hostutils = utilsfactory.get_hostutils()
        self._pathutils = pathutils.PathUtils()
        self._volumeops = volumeops.VolumeOps()
        self._imagecache = imagecache.ImageCache()
        self._serial_console_ops = serialconsoleops.SerialConsoleOps()
        self._vif_driver = None
        self._load_vif_driver_class()

    def _load_vif_driver_class(self):
        try:
            class_name = get_network_driver()
            self._vif_driver = importutils.import_object(class_name)
        except KeyError:
            raise TypeError(_("VIF driver not found for "
                              "network_api_class: %s") %
                            CONF.network_api_class)

    def list_instance_uuids(self):
        instance_uuids = []
        for (instance_name, notes) in self._vmutils.list_instance_notes():
            if notes and uuidutils.is_uuid_like(notes[0]):
                instance_uuids.append(str(notes[0]))
            else:
                LOG.debug("Notes not found or not resembling a GUID for "
                          "instance: %s", instance_name)
        return instance_uuids

    def list_instances(self):
        return self._vmutils.list_instances()

    def estimate_instance_overhead(self, instance_info):
        # NOTE(claudiub): When an instance starts, Hyper-V creates a VM memory
        # file on the local disk. The file size is the same as the VM's amount
        # of memory. Since disk_gb must be an integer, and memory is MB, round
        # up from X512 MB.
        return {'memory_mb': 0,
                'disk_gb': (instance_info['memory_mb'] + 512) // units.Ki}

    def get_info(self, instance):
        """Get information about the VM."""
        LOG.debug("get_info called for instance", instance=instance)

        instance_name = instance.name
        if not self._vmutils.vm_exists(instance_name):
            raise exception.InstanceNotFound(instance_id=instance.uuid)

        info = self._vmutils.get_vm_summary_info(instance_name)

        state = constants.HYPERV_POWER_STATE[info['EnabledState']]
        return hardware.InstanceInfo(state=state,
                                     max_mem_kb=info['MemoryUsage'],
                                     mem_kb=info['MemoryUsage'],
                                     num_cpu=info['NumberOfProcessors'],
                                     cpu_time_ns=info['UpTime'])

    def _create_root_vhd(self, context, instance, rescue_image_id=None):
        is_rescue_vhd = rescue_image_id is not None

        base_vhd_path = self._imagecache.get_cached_image(context, instance,
                                                          rescue_image_id)
        base_vhd_info = self._vhdutils.get_vhd_info(base_vhd_path)
        base_vhd_size = base_vhd_info['VirtualSize']
        format_ext = base_vhd_path.split('.')[-1]
        root_vhd_path = self._pathutils.get_root_vhd_path(instance.name,
                                                          format_ext,
                                                          is_rescue_vhd)
        root_vhd_size = instance.root_gb * units.Gi

        try:
            if CONF.use_cow_images:
                LOG.debug("Creating differencing VHD. Parent: "
                          "%(base_vhd_path)s, Target: %(root_vhd_path)s",
                          {'base_vhd_path': base_vhd_path,
                           'root_vhd_path': root_vhd_path},
                          instance=instance)
                self._vhdutils.create_differencing_vhd(root_vhd_path,
                                                       base_vhd_path)
                vhd_type = self._vhdutils.get_vhd_format(base_vhd_path)
                if vhd_type == constants.DISK_FORMAT_VHD:
                    # The base image has already been resized. As differencing
                    # vhdx images support it, the root image will be resized
                    # instead if needed.
                    return root_vhd_path
            else:
                LOG.debug("Copying VHD image %(base_vhd_path)s to target: "
                          "%(root_vhd_path)s",
                          {'base_vhd_path': base_vhd_path,
                           'root_vhd_path': root_vhd_path},
                          instance=instance)
                self._pathutils.copyfile(base_vhd_path, root_vhd_path)

            root_vhd_internal_size = (
                self._vhdutils.get_internal_vhd_size_by_file_size(
                    base_vhd_path, root_vhd_size))

            if not is_rescue_vhd and self._is_resize_needed(
                    root_vhd_path, base_vhd_size,
                    root_vhd_internal_size, instance):
                self._vhdutils.resize_vhd(root_vhd_path,
                                          root_vhd_internal_size,
                                          is_file_max_size=False)
        except Exception:
            with excutils.save_and_reraise_exception():
                if self._pathutils.exists(root_vhd_path):
                    self._pathutils.remove(root_vhd_path)

        return root_vhd_path

    def _is_resize_needed(self, vhd_path, old_size, new_size, instance):
        if new_size < old_size:
            raise exception.FlavorDiskSmallerThanImage(
                flavor_size=new_size, image_size=old_size)
        elif new_size > old_size:
            LOG.debug("Resizing VHD %(vhd_path)s to new "
                      "size %(new_size)s",
                      {'new_size': new_size,
                       'vhd_path': vhd_path},
                      instance=instance)
            return True
        return False

    def create_ephemeral_vhd(self, instance):
        eph_vhd_size = instance.get('ephemeral_gb', 0) * units.Gi
        if eph_vhd_size:
            vhd_format = self._vhdutils.get_best_supported_vhd_format()

            eph_vhd_path = self._pathutils.get_ephemeral_vhd_path(
                instance.name, vhd_format)
            self._vhdutils.create_dynamic_vhd(eph_vhd_path, eph_vhd_size)
            return eph_vhd_path

    @check_admin_permissions
    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, network_info, block_device_info=None):
        """Create a new VM and start it."""
        LOG.info(_LI("Spawning new instance"), instance=instance)

        instance_name = instance.name
        if self._vmutils.vm_exists(instance_name):
            raise exception.InstanceExists(name=instance_name)

        # Make sure we're starting with a clean slate.
        self._delete_disk_files(instance_name)

        if self._volumeops.ebs_root_in_block_devices(block_device_info):
            root_vhd_path = None
        else:
            root_vhd_path = self._create_root_vhd(context, instance)

        eph_vhd_path = self.create_ephemeral_vhd(instance)
        vm_gen = self.get_image_vm_generation(
            instance.uuid, root_vhd_path, image_meta)

        try:
            self.create_instance(instance, network_info, block_device_info,
                                 root_vhd_path, eph_vhd_path,
                                 vm_gen)

            if configdrive.required_by(instance):
                configdrive_path = self._create_config_drive(instance,
                                                             injected_files,
                                                             admin_password,
                                                             network_info)

                self.attach_config_drive(instance, configdrive_path, vm_gen)

            self.power_on(instance)
        except Exception:
            with excutils.save_and_reraise_exception():
                self.destroy(instance)

    def create_instance(self, instance, network_info, block_device_info,
                        root_vhd_path, eph_vhd_path, vm_gen):
        instance_name = instance.name
        instance_path = os.path.join(CONF.instances_path, instance_name)

        self._vmutils.create_vm(instance_name,
                                instance.memory_mb,
                                instance.vcpus,
                                CONF.hyperv.limit_cpu_features,
                                CONF.hyperv.dynamic_memory_ratio,
                                vm_gen,
                                instance_path,
                                [instance.uuid])

        self._vmutils.create_scsi_controller(instance_name)
        controller_type = VM_GENERATIONS_CONTROLLER_TYPES[vm_gen]

        ctrl_disk_addr = 0
        if root_vhd_path:
            self._attach_drive(instance_name, root_vhd_path, 0, ctrl_disk_addr,
                               controller_type)

        ctrl_disk_addr = 1
        if eph_vhd_path:
            self._attach_drive(instance_name, eph_vhd_path, 0, ctrl_disk_addr,
                               controller_type)

        # If ebs_root is False, the first volume will be attached to SCSI
        # controller. Generation 2 VMs only has a SCSI controller.
        ebs_root = vm_gen is not constants.VM_GEN_2 and root_vhd_path is None
        self._volumeops.attach_volumes(block_device_info,
                                       instance_name,
                                       ebs_root)

        # For the moment, we use COM port 1 when getting the serial console
        # log as well as interactive sessions. In the future, the way in which
        # we consume instance serial ports may become configurable.
        #
        # Note that Hyper-V instances will always have 2 COM ports
        serial_ports = {
            constants.DEFAULT_SERIAL_CONSOLE_PORT:
                constants.SERIAL_PORT_TYPE_RW}
        self._create_vm_com_port_pipes(instance, serial_ports)

        for vif in network_info:
            LOG.debug('Creating nic for instance', instance=instance)
            self._vmutils.create_nic(instance_name,
                                     vif['id'],
                                     vif['address'])
            self._vif_driver.plug(instance, vif)

        if CONF.hyperv.enable_instance_metrics_collection:
            self._metricsutils.enable_vm_metrics_collection(instance_name)

    def _attach_drive(self, instance_name, path, drive_addr, ctrl_disk_addr,
                      controller_type, drive_type=constants.DISK):
        if controller_type == constants.CTRL_TYPE_SCSI:
            self._vmutils.attach_scsi_drive(instance_name, path, drive_type)
        else:
            self._vmutils.attach_ide_drive(instance_name, path, drive_addr,
                                           ctrl_disk_addr, drive_type)

    def get_image_vm_generation(self, instance_id, root_vhd_path, image_meta):
        default_vm_gen = self._hostutils.get_default_vm_generation()
        image_prop_vm = image_meta.properties.get(
            'hw_machine_type', default_vm_gen)
        if image_prop_vm not in self._hostutils.get_supported_vm_types():
            reason = _LE('Requested VM Generation %s is not supported on '
                         'this OS.') % image_prop_vm
            raise exception.InstanceUnacceptable(instance_id=instance_id,
                                                 reason=reason)

        vm_gen = VM_GENERATIONS[image_prop_vm]

        if (vm_gen != constants.VM_GEN_1 and root_vhd_path and
                self._vhdutils.get_vhd_format(
                    root_vhd_path) == constants.DISK_FORMAT_VHD):
            reason = _LE('Requested VM Generation %s, but provided VHD '
                         'instead of VHDX.') % vm_gen
            raise exception.InstanceUnacceptable(instance_id=instance_id,
                                                 reason=reason)

        return vm_gen

    def _create_config_drive(self, instance, injected_files, admin_password,
                             network_info, rescue=False):
        if CONF.config_drive_format != 'iso9660':
            raise exception.ConfigDriveUnsupportedFormat(
                format=CONF.config_drive_format)

        LOG.info(_LI('Using config drive for instance'), instance=instance)

        extra_md = {}
        if admin_password and CONF.hyperv.config_drive_inject_password:
            extra_md['admin_pass'] = admin_password

        inst_md = instance_metadata.InstanceMetadata(instance,
                                                     content=injected_files,
                                                     extra_md=extra_md,
                                                     network_info=network_info)

        configdrive_path_iso = self._pathutils.get_configdrive_path(
            instance.name, constants.DVD_FORMAT, rescue=rescue)
        LOG.info(_LI('Creating config drive at %(path)s'),
                 {'path': configdrive_path_iso}, instance=instance)

        with configdrive.ConfigDriveBuilder(instance_md=inst_md) as cdb:
            try:
                cdb.make_drive(configdrive_path_iso)
            except processutils.ProcessExecutionError as e:
                with excutils.save_and_reraise_exception():
                    LOG.error(_LE('Creating config drive failed with '
                                  'error: %s'),
                              e, instance=instance)

        if not CONF.hyperv.config_drive_cdrom:
            configdrive_path = self._pathutils.get_configdrive_path(
                instance.name, constants.DISK_FORMAT_VHD, rescue=rescue)
            utils.execute(CONF.hyperv.qemu_img_cmd,
                          'convert',
                          '-f',
                          'raw',
                          '-O',
                          'vpc',
                          configdrive_path_iso,
                          configdrive_path,
                          attempts=1)
            self._pathutils.remove(configdrive_path_iso)
        else:
            configdrive_path = configdrive_path_iso

        return configdrive_path

    def attach_config_drive(self, instance, configdrive_path, vm_gen):
        configdrive_ext = configdrive_path[(configdrive_path.rfind('.') + 1):]
        # Do the attach here and if there is a certain file format that isn't
        # supported in constants.DISK_FORMAT_MAP then bomb out.
        try:
            drive_type = constants.DISK_FORMAT_MAP[configdrive_ext]
            controller_type = VM_GENERATIONS_CONTROLLER_TYPES[vm_gen]
            self._attach_drive(instance.name, configdrive_path, 1, 0,
                               controller_type, drive_type)
        except KeyError:
            raise exception.InvalidDiskFormat(disk_format=configdrive_ext)

    def _detach_config_drive(self, instance_name, rescue=False, delete=False):
        configdrive_path = self._pathutils.lookup_configdrive_path(
            instance_name, rescue=rescue)

        if configdrive_path:
            self._vmutils.detach_vm_disk(instance_name,
                                         configdrive_path,
                                         is_physical=False)
            if delete:
                self._pathutils.remove(configdrive_path)

    def _delete_disk_files(self, instance_name):
        self._pathutils.get_instance_dir(instance_name,
                                         create_dir=False,
                                         remove_dir=True)

    def destroy(self, instance, network_info=None, block_device_info=None,
                destroy_disks=True):
        instance_name = instance.name
        LOG.info(_LI("Got request to destroy instance"), instance=instance)
        try:
            if self._vmutils.vm_exists(instance_name):

                # Stop the VM first.
                self._vmutils.stop_vm_jobs(instance_name)
                self.power_off(instance)

                if network_info:
                    for vif in network_info:
                        self._vif_driver.unplug(instance, vif)

                self._vmutils.destroy_vm(instance_name)
                self._volumeops.disconnect_volumes(block_device_info)
            else:
                LOG.debug("Instance not found", instance=instance)

            if destroy_disks:
                self._delete_disk_files(instance_name)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception(_LE('Failed to destroy instance: %s'),
                              instance_name)

    def reboot(self, instance, network_info, reboot_type):
        """Reboot the specified instance."""
        LOG.debug("Rebooting instance", instance=instance)

        if reboot_type == REBOOT_TYPE_SOFT:
            if self._soft_shutdown(instance):
                self.power_on(instance)
                return

        self._set_vm_state(instance,
                           os_win_const.HYPERV_VM_STATE_REBOOT)

    def _soft_shutdown(self, instance,
                       timeout=CONF.hyperv.wait_soft_reboot_seconds,
                       retry_interval=SHUTDOWN_TIME_INCREMENT):
        """Perform a soft shutdown on the VM.

           :return: True if the instance was shutdown within time limit,
                    False otherwise.
        """
        LOG.debug("Performing Soft shutdown on instance", instance=instance)

        while timeout > 0:
            # Perform a soft shutdown on the instance.
            # Wait maximum timeout for the instance to be shutdown.
            # If it was not shutdown, retry until it succeeds or a maximum of
            # time waited is equal to timeout.
            wait_time = min(retry_interval, timeout)
            try:
                LOG.debug("Soft shutdown instance, timeout remaining: %d",
                          timeout, instance=instance)
                self._vmutils.soft_shutdown_vm(instance.name)
                if self._wait_for_power_off(instance.name, wait_time):
                    LOG.info(_LI("Soft shutdown succeeded."),
                             instance=instance)
                    return True
            except os_win_exc.HyperVException as e:
                # Exception is raised when trying to shutdown the instance
                # while it is still booting.
                LOG.debug("Soft shutdown failed: %s", e, instance=instance)
                time.sleep(wait_time)

            timeout -= retry_interval

        LOG.warning(_LW("Timed out while waiting for soft shutdown."),
                    instance=instance)
        return False

    def pause(self, instance):
        """Pause VM instance."""
        LOG.debug("Pause instance", instance=instance)
        self._set_vm_state(instance,
                           os_win_const.HYPERV_VM_STATE_PAUSED)

    def unpause(self, instance):
        """Unpause paused VM instance."""
        LOG.debug("Unpause instance", instance=instance)
        self._set_vm_state(instance,
                           os_win_const.HYPERV_VM_STATE_ENABLED)

    def suspend(self, instance):
        """Suspend the specified instance."""
        LOG.debug("Suspend instance", instance=instance)
        self._set_vm_state(instance,
                           os_win_const.HYPERV_VM_STATE_SUSPENDED)

    def resume(self, instance):
        """Resume the suspended VM instance."""
        LOG.debug("Resume instance", instance=instance)
        self._set_vm_state(instance,
                           os_win_const.HYPERV_VM_STATE_ENABLED)

    def power_off(self, instance, timeout=0, retry_interval=0):
        """Power off the specified instance."""
        LOG.debug("Power off instance", instance=instance)

        # We must make sure that the console log workers are stopped,
        # otherwise we won't be able to delete or move the VM log files.
        self._serial_console_ops.stop_console_handler(instance.name)

        if retry_interval <= 0:
            retry_interval = SHUTDOWN_TIME_INCREMENT

        try:
            if timeout and self._soft_shutdown(instance,
                                               timeout,
                                               retry_interval):
                return

            self._set_vm_state(instance,
                               os_win_const.HYPERV_VM_STATE_DISABLED)
        except os_win_exc.HyperVVMNotFoundException:
            # The manager can call the stop API after receiving instance
            # power off events. If this is triggered when the instance
            # is being deleted, it might attempt to power off an unexisting
            # instance. We'll just pass in this case.
            LOG.debug("Instance not found. Skipping power off",
                      instance=instance)

    def power_on(self, instance, block_device_info=None):
        """Power on the specified instance."""
        LOG.debug("Power on instance", instance=instance)

        if block_device_info:
            self._volumeops.fix_instance_volume_disk_paths(instance.name,
                                                           block_device_info)

        self._set_vm_state(instance, os_win_const.HYPERV_VM_STATE_ENABLED)

    def _set_vm_state(self, instance, req_state):
        instance_name = instance.name

        try:
            self._vmutils.set_vm_state(instance_name, req_state)

            LOG.debug("Successfully changed state of VM %(instance_name)s"
                      " to: %(req_state)s", {'instance_name': instance_name,
                                             'req_state': req_state})
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE("Failed to change vm state of %(instance_name)s"
                              " to %(req_state)s"),
                          {'instance_name': instance_name,
                           'req_state': req_state})

    def _get_vm_state(self, instance_name):
        summary_info = self._vmutils.get_vm_summary_info(instance_name)
        return summary_info['EnabledState']

    def _wait_for_power_off(self, instance_name, time_limit):
        """Waiting for a VM to be in a disabled state.

           :return: True if the instance is shutdown within time_limit,
                    False otherwise.
        """

        desired_vm_states = [os_win_const.HYPERV_VM_STATE_DISABLED]

        def _check_vm_status(instance_name):
            if self._get_vm_state(instance_name) in desired_vm_states:
                raise loopingcall.LoopingCallDone()

        periodic_call = loopingcall.FixedIntervalLoopingCall(_check_vm_status,
                                                             instance_name)

        try:
            # add a timeout to the periodic call.
            periodic_call.start(interval=SHUTDOWN_TIME_INCREMENT)
            etimeout.with_timeout(time_limit, periodic_call.wait)
        except etimeout.Timeout:
            # VM did not shutdown in the expected time_limit.
            return False
        finally:
            # stop the periodic call, in case of exceptions or Timeout.
            periodic_call.stop()

        return True

    def resume_state_on_host_boot(self, context, instance, network_info,
                                  block_device_info=None):
        """Resume guest state when a host is booted."""
        self.power_on(instance, block_device_info)

    def _create_vm_com_port_pipes(self, instance, serial_ports):
        for port_number, port_type in serial_ports.items():
            pipe_path = r'\\.\pipe\%s_%s' % (instance.uuid, port_type)
            self._vmutils.set_vm_serial_port_connection(
                instance.name, port_number, pipe_path)

    def copy_vm_dvd_disks(self, vm_name, dest_host):
        dvd_disk_paths = self._vmutils.get_vm_dvd_disk_paths(vm_name)
        dest_path = self._pathutils.get_instance_dir(
            vm_name, remote_server=dest_host)
        for path in dvd_disk_paths:
            self._pathutils.copyfile(path, dest_path)

    def _check_hotplug_available(self, instance):
        """Check whether attaching an interface is possible for the given
        instance.

        :returns: True if attaching / detaching interfaces is possible for the
                  given instance.
        """
        vm_state = self._get_vm_state(instance.name)
        if vm_state == os_win_const.HYPERV_VM_STATE_DISABLED:
            # can attach / detach interface to stopped VMs.
            return True

        if not self._hostutils.check_min_windows_version(10, 0):
            # TODO(claudiub): add set log level to error after string freeze.
            LOG.debug("vNIC hot plugging is supported only in newer "
                      "versions than Windows Hyper-V / Server 2012 R2.")
            return False

        if (self._vmutils.get_vm_generation(instance.name) ==
                constants.VM_GEN_1):
            # TODO(claudiub): add set log level to error after string freeze.
            LOG.debug("Cannot hot plug vNIC to a first generation VM.",
                      instance=instance)
            return False

        return True

    def attach_interface(self, instance, vif):
        if not self._check_hotplug_available(instance):
            raise exception.InterfaceAttachFailed(instance_uuid=instance.uuid)

        LOG.debug('Attaching vif: %s', vif['id'], instance=instance)
        self._vmutils.create_nic(instance.name, vif['id'], vif['address'])
        self._vif_driver.plug(instance, vif)

    def detach_interface(self, instance, vif):
        try:
            if not self._check_hotplug_available(instance):
                raise exception.InterfaceDetachFailed(
                    instance_uuid=instance.uuid)

            LOG.debug('Detaching vif: %s', vif['id'], instance=instance)
            self._vif_driver.unplug(instance, vif)
            self._vmutils.destroy_nic(instance.name, vif['id'])
        except os_win_exc.HyperVVMNotFoundException:
            # TODO(claudiub): add set log level to error after string freeze.
            LOG.debug("Instance not found during detach interface. It "
                      "might have been destroyed beforehand.",
                      instance=instance)
            raise exception.InterfaceDetachFailed(instance_uuid=instance.uuid)

    def rescue_instance(self, context, instance, network_info, image_meta,
                        rescue_password):
        try:
            self._rescue_instance(context, instance, network_info,
                                  image_meta, rescue_password)
        except Exception as exc:
            with excutils.save_and_reraise_exception():
                err_msg = _LE("Instance rescue failed. Exception: %(exc)s. "
                              "Attempting to unrescue the instance.")
                LOG.error(err_msg, {'exc': exc}, instance=instance)
                self.unrescue_instance(instance)

    def _rescue_instance(self, context, instance, network_info, image_meta,
                         rescue_password):
        rescue_image_id = image_meta.id or instance.image_ref
        rescue_vhd_path = self._create_root_vhd(
            context, instance, rescue_image_id=rescue_image_id)

        rescue_vm_gen = self.get_image_vm_generation(instance.uuid,
                                                     rescue_vhd_path,
                                                     image_meta)
        vm_gen = self._vmutils.get_vm_generation(instance.name)
        if rescue_vm_gen != vm_gen:
            err_msg = _('The requested rescue image requires a different VM '
                        'generation than the actual rescued instance. '
                        'Rescue image VM generation: %(rescue_vm_gen)s. '
                        'Rescued instance VM generation: %(vm_gen)s.') % dict(
                            rescue_vm_gen=rescue_vm_gen,
                            vm_gen=vm_gen)
            raise exception.ImageUnacceptable(reason=err_msg,
                                              image_id=rescue_image_id)

        root_vhd_path = self._pathutils.lookup_root_vhd_path(instance.name)
        if not root_vhd_path:
            err_msg = _('Instance root disk image could not be found. '
                        'Rescuing instances booted from volume is '
                        'not supported.')
            raise exception.InstanceNotRescuable(reason=err_msg,
                                                 instance_id=instance.uuid)

        controller_type = VM_GENERATIONS_CONTROLLER_TYPES[vm_gen]

        self._vmutils.detach_vm_disk(instance.name, root_vhd_path,
                                     is_physical=False)
        self._attach_drive(instance.name, rescue_vhd_path, 0,
                           self._ROOT_DISK_CTRL_ADDR, controller_type)
        self._vmutils.attach_scsi_drive(instance.name, root_vhd_path,
                                        drive_type=constants.DISK)

        if configdrive.required_by(instance):
            self._detach_config_drive(instance.name)
            rescue_configdrive_path = self._create_config_drive(
                instance,
                injected_files=None,
                admin_password=rescue_password,
                network_info=network_info,
                rescue=True)
            self.attach_config_drive(instance, rescue_configdrive_path,
                                     vm_gen)

        self.power_on(instance)

    def unrescue_instance(self, instance):
        self.power_off(instance)

        root_vhd_path = self._pathutils.lookup_root_vhd_path(instance.name)
        rescue_vhd_path = self._pathutils.lookup_root_vhd_path(instance.name,
                                                               rescue=True)

        if (instance.vm_state == vm_states.RESCUED and
                not (rescue_vhd_path and root_vhd_path)):
            err_msg = _('Missing instance root and/or rescue image. '
                        'The instance cannot be unrescued.')
            raise exception.InstanceNotRescuable(reason=err_msg,
                                                 instance_id=instance.uuid)

        vm_gen = self._vmutils.get_vm_generation(instance.name)
        controller_type = VM_GENERATIONS_CONTROLLER_TYPES[vm_gen]

        self._vmutils.detach_vm_disk(instance.name, root_vhd_path,
                                     is_physical=False)
        if rescue_vhd_path:
            self._vmutils.detach_vm_disk(instance.name, rescue_vhd_path,
                                         is_physical=False)
            fileutils.delete_if_exists(rescue_vhd_path)
        self._attach_drive(instance.name, root_vhd_path, 0,
                           self._ROOT_DISK_CTRL_ADDR, controller_type)

        self._detach_config_drive(instance.name, rescue=True, delete=True)

        # Reattach the configdrive, if exists and not already attached.
        configdrive_path = self._pathutils.lookup_configdrive_path(
            instance.name)
        if configdrive_path and not self._vmutils.is_disk_attached(
                configdrive_path, is_physical=False):
            self.attach_config_drive(instance, configdrive_path, vm_gen)

        self.power_on(instance)
