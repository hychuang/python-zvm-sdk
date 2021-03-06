

import time
import os
from log import LOG
import uuid
import time
import utils as zvmutils
import constants as const
from utils import ZVMException, get_xcat_url
import dist
import six
import configdrive


import os
import commands

from config import CONF
from log import LOG
import utils as zvmutils
import constants as const

_VMOPS = None


def _get_vmops():
    global _VMOPS
    if _VMOPS is None:
        _VMOPS = VMOps()
    return _VMOPS

def run_instance(instance_name, image_name, cpu, memory,
                 login_password, ip_addr):
    """Deploy and provision a virtual machine.

    Input parameters:
    :instance_name:   USERID of the instance, no more than 8.
    :image_name:      e.g. rhel7.2-s390x-netboot-7e5efe8a_9f4e_11e6_b85d_02000b000015
    :cpu:             vcpu
    :memory:          memory
    :login_password:  login password
    :ip_addr:         ip address
    """
    vmops = _get_vmops()

    os_version = zvmutils.get_image_version(image_name)

    # For zVM instance, limit the maximum length of instance name to be 8
    if len(instance_name) > 8:
        msg = (("Don't support spawn vm on zVM hypervisor with instance "
            "name: %s, please change your instance name no longer than 8 "
            "characters") % instance_name)
        raise ZVMException(msg)

    instance_path = zvmutils.get_instance_path(CONF.zvm_host, instance_name)
    linuxdist = vmops._dist_manager.get_linux_dist(os_version)()
    transportfiles = configdrive.create_config_drive(ip_addr, os_version)

    spawn_start = time.time()

    # Create xCAT node and userid for the instance
    zvmutils.create_xcat_node(instance_name, CONF.zhcp)
    vmops.create_userid(instance_name, cpu, memory, image_name)

    # Setup network for z/VM instance
    vmops._preset_instance_network(instance_name, ip_addr)
    vmops._add_nic_to_table(instance_name, ip_addr)
    zvmutils.update_node_info(instance_name, image_name, os_version)
    zvmutils.deploy_node(instance_name, image_name, transportfiles)

    # Change vm's admin password during spawn
    zvmutils.punch_adminpass_file(instance_path, instance_name,
                                  login_password, linuxdist)
    # Unlock the instance
    zvmutils.punch_xcat_auth_file(instance_path, instance_name)

    # Power on the instance, then put MN's public key into instance
    vmops.power_on(instance_name)
    spawn_time = time.time() - spawn_start
    LOG.info("Instance spawned succeeded in %s seconds", spawn_time)

    return instance_name

def terminate_instance(instance_name):
    """Destroy a virtual machine.

    Input parameters:
    :instance_name:   USERID of the instance, last 8 if length > 8
    """
    vmops = _get_vmops()
    if vmops.instance_exists(instance_name):
        LOG.info(("Destroying instance %s"), instance_name)
        if vmops.is_reachable(instance_name):
            LOG.debug(("Node %s is reachable, "
                      "skipping diagnostics collection"), instance_name)
        elif vmops.is_powered_off(instance_name):
            LOG.debug(("Node %s is powered off, "
                      "skipping diagnostics collection"), instance_name)
        else:
            LOG.debug(("Node %s is powered on but unreachable"), instance_name)

    zvmutils.clean_mac_switch_host(instance_name)

    vmops.delete_userid(instance_name, CONF.zhcp)


def describe_instance(instance_name):
    """Get virtual machine basic information.

    Input parameters:
    :instance_name:   USERID of the instance, last 8 if length > 8
    """
    inst_info = _get_vmops().get_info(instance_name)
    return inst_info


def start_instance(instance_name):
    """Power on a virtual machine.

    Input parameters:
    :instance_name:   USERID of the instance, last 8 if length > 8
    """
    _get_vmops()._power_state(instance_name, "PUT", "on")


def stop_instance(instance_name):
    """Shutdown a virtual machine.

    Input parameters:
    :instance_name:   USERID of the instance, last 8 if length > 8
    """
    _get_vmops()._power_state(instance_name, "PUT", "off")


def create_volume(size):
    """Create a volume.

    Input parameters:
    :size:           size

    Output parameters:
    :volume_uuid:    volume uuid in zVM
    """
    volumeops = _get_volumeops()
    volume_uuid = ""

    action = '--add9336'
    diskpool = CONF.volume_diskpool
    vdev = volumeops.get_free_mgr_vdev()
    multipass = const.VOLUME_MULTI_PASS
    fmt = CONF.volume_filesystem
    body = [" ".join([action, diskpool, vdev, str(size), "MR", "read", "write",
                      multipass, fmt])]
    url = zvmutils.get_xcat_url().chvm('/' + CONF.volume_mgr_node)
    # Update the volume management file before sending xcat request
    volumeops.add_volume_info(" ".join([vdev, "free",
                                     CONF.volume_mgr_userid, vdev]))
    zvmutils.xcat_request("PUT", url, body)
    volume_uuid = vdev
    return volume_uuid


def delete_volume(volume_uuid):
    """Delete a volume.

    Input parameters:
    :volume_uuid:    volume uuid in zVM
    """
    volumeops = _get_volumeops()
    volume_info = volumeops.get_volume_info(volume_uuid)
    # Check volume status
    if (volume_info is None):
        msg = ("Volume %s does not exist.") % volume_uuid
        raise zvmutils.ZVMException(msg)
    if (volume_info['status'] == 'in-use'):
        msg = ("Cann't delete volume %(uuid)s, attached to "
               "instance %(vm)s" % {'uuid': volume_uuid,
                                    'vm': volume_info['userid']})
        raise zvmutils.ZVMException(msg)
    # Delete volume from volume manager user
    action = '--removedisk'
    vdev = volume_uuid
    body = [" ".join([action, vdev])]
    url = zvmutils.get_xcat_url().chvm('/' + CONF.volume_mgr_node)
    zvmutils.xcat_request("PUT", url, body)
    # Delete volume from volume management file
    volumeops.delete_volume_info(volume_uuid)


def attach_volume(instance_name, volume_uuid):
    """Attach a volume to a target vm.

    Input parameters:
    :instance_name:   USERID of the instance, last 8 if length > 8
    ::volume_uuid:    volume uuid in zVM
    """
    volumeops = _get_volumeops()
    volume_info = volumeops.get_volume_info(volume_uuid)
    if (volume_info is None) or (volume_info['status'] != 'free'):
        msg = ("Volume %s does not exist or status is not free.") % volume_uuid
        raise zvmutils.ZVMException(msg)
    target_vdev = volumeops.get_free_vdev(instance_name)
    # First update the status in the management file and then call smcli
    volumeops.update_volume_info(" ".join([volume_uuid, "in-use",
                                          instance_name, target_vdev]))
    cmd = ("/opt/zhcp/bin/smcli Image_Disk_Share_DM -T %(dst)s"
           " -v %(dst_vdev)s -t %(src)s -r %(src_vdev)s -a MR"
           " -p multi" % {'dst': instance_name, 'dst_vdev': target_vdev,
           'src': CONF.volume_mgr_userid, 'src_vdev': volume_uuid})
    zhcp_node = CONF.zvm_zhcp_node
    zvmutils.xdsh(zhcp_node, cmd)
    cmd = ("/opt/zhcp/bin/smcli Image_Disk_Create -T %(dst)s -v %(dst_vdev)s"
           " -m MR" % {'dst': instance_name, 'dst_vdev': target_vdev})
    zvmutils.xdsh(zhcp_node, cmd)


def detach_volume(instance_name, volume_uuid):
    """Detach a volume.

    Input parameters:
    :instance_name:   USERID of the instance, last 8 if length > 8
    :volume_uuid:    volume uuid in zVM
    """
    volumeops = _get_volumeops()
    volume_info = volumeops.get_volume_info(volume_uuid)
    # Check volume status
    if (volume_info is None):
        msg = ("Volume %s does not exist.") % volume_uuid
        raise zvmutils.ZVMException(msg)
    if (volume_info['status'] != 'in-use') or (
        volume_info['userid'] != instance_name):
        msg = ("Volume %(uuid)s is not attached to"
               "instance %(vm)s" % {'uuid': volume_uuid, 'vm': instance_name})
        raise zvmutils.ZVMException(msg)
    # Detach volume
    cmd = ("/opt/zhcp/bin/smcli Image_Disk_Unshare_DM -T %(dst)s"
       " -v %(dst_vdev)s -t %(src)s -r %(src_vdev)s" %
       {'dst': instance_name, 'dst_vdev': volume_info['vdev'],
       'src': CONF.volume_mgr_userid, 'src_vdev': volume_uuid})
    zhcp_node = CONF.zvm_zhcp_node
    zvmutils.xdsh(zhcp_node, cmd)
    cmd = ("/opt/zhcp/bin/smcli Image_Disk_Delete -T %(dst)s -v %(dst_vdev)s"
           % {'dst': instance_name, 'dst_vdev': volume_info['vdev']})
    zvmutils.xdsh(zhcp_node, cmd)
    # Update volume status to free in management file
    volumeops.update_volume_info(" ".join([volume_uuid, "free",
                                        CONF.volume_mgr_userid, volume_uuid]))


def capture_instance(instance_name):
    """Caputre a virtual machine image.

    Input parameters:
    :instance_name:   USERID of the instance, last 8 if length > 8

    Output parameters:
    :image_name:      Image name that defined in xCAT image repo
    """
    _vmops = _get_vmops()
    if _vmops.get_power_state(instance_name) == "off":
        _vmops.power_on(instance_name)

    return _vmops.capture_instance(instance_name)


def delete_image(image_name):
    """Delete image.

    Input parameters:
    :image_name:      Image name that defined in xCAT image repo
    """
    _get_vmops().delete_image(image_name)


class VMOps(object):

    def __init__(self):
        self._xcat_url = zvmutils.get_xcat_url()            
        self._dist_manager = dist.ListDistManager()

    def _power_state(self, instance_name, method, state):
        """Invoke xCAT REST API to set/get power state for a instance."""
        body = [state]
        url = self._xcat_url.rpower('/' + instance_name)
        return zvmutils.xcat_request(method, url, body)

    def get_power_state(self, instance_name):
        """Get power status of a z/VM instance."""
        LOG.debug('Query power stat of %s' % instance_name)
        res_dict = self._power_state(instance_name, "GET", "stat")

        @zvmutils.wrap_invalid_xcat_resp_data_error
        def _get_power_string(d):
            tempstr = d['info'][0][0]
            return tempstr[(tempstr.find(':') + 2):].strip()

        power_stat = _get_power_string(res_dict)
        return power_stat

    @zvmutils.wrap_invalid_xcat_resp_data_error
    def _lsdef(self, instance_name):
        url = self._xcat_url.lsdef_node('/' + instance_name)
        resp_info = zvmutils.xcat_request("GET", url)['info'][0]
        return resp_info

    @zvmutils.wrap_invalid_xcat_resp_data_error
    def _get_ip_addr_from_lsdef_info(self, info):
        for inf in info:
            if 'ip=' in inf:
                ip_addr = inf.rpartition('ip=')[2].strip(' \n')
                return ip_addr

    @zvmutils.wrap_invalid_xcat_resp_data_error
    def _get_os_from_lsdef_info(self, info):
        for inf in info:
            if 'os=' in inf:
                _os = inf.rpartition('os=')[2].strip(' \n')
                return _os

    @zvmutils.wrap_invalid_xcat_resp_data_error
    def _lsvm(self, instance_name):
        url = self._xcat_url.lsvm('/' + instance_name)
        resp_info = zvmutils.xcat_request("GET", url)['info'][0][0]
        return resp_info.split('\n')

    @zvmutils.wrap_invalid_xcat_resp_data_error
    def _get_cpu_num_from_lsvm_info(self, info):
        cpu_num = 0
        for inf in info:
            if ': CPU ' in inf:
                cpu_num += 1
        return cpu_num

    @zvmutils.wrap_invalid_xcat_resp_data_error
    def _get_memory_from_lsvm_info(self, info):
        return info[0].split(' ')[4]

    def get_info(self, instance_name):
        power_stat = self.get_power_state(instance_name)

        lsdef_info = self._lsdef(instance_name)
        ip_addr = self._get_ip_addr_from_lsdef_info(lsdef_info)
        _os = self._get_os_from_lsdef_info(lsdef_info)

        lsvm_info = self._lsvm(instance_name)
        vcpus = self._get_cpu_num_from_lsvm_info(lsvm_info)
        mem = self._get_memory_from_lsvm_info(lsvm_info)

        return {'power_state': power_stat,
                'vcpus': vcpus,
                'memory': mem,
                'ip_addr': ip_addr,
                'os': _os}

    def instance_metadata(self, instance, content, extra_md):
        pass

    def add_instance_metadata(self):
        pass

    def _preset_instance_network(self, instance_name, ip_addr):
        zvmutils.config_xcat_mac(instance_name)
        LOG.debug("Add ip/host name on xCAT MN for instance %s",
                    instance_name)

        zvmutils.add_xcat_host(instance_name, ip_addr, instance_name)
        zvmutils.makehosts()

    def _add_nic_to_table(self, instance_name, ip_addr):
        nic_vdev = CONF.zvm_default_nic_vdev
        nic_name = CONF.nic_name
        zhcpnode = CONF.zhcp
        zvmutils.create_xcat_table_about_nic(zhcpnode,
                                         instance_name,
                                         nic_name,
                                         ip_addr,
                                         nic_vdev)
        nic_vdev = str(hex(int(nic_vdev, 16) + 3))[2:]            

    def _wait_for_reachable(self, instance_name):
        """Called at an interval until the instance is reachable."""
        self._reachable = False

        def _check_reachable():
            if not self.is_reachable(instance_name):
                pass
            else:
                self._reachable = True

        zvmutils.looping_call(_check_reachable, 5, 5, 30,
                              CONF.zvm_reachable_timeout,
                              ZVMException(msg='not reachable, retry'))

    def is_reachable(self, instance_name):
        """Return True is the instance is reachable."""
        url = self._xcat_url.nodestat('/' + instance_name)
        LOG.debug('Get instance status of %s', instance_name)
        res_dict = zvmutils.xcat_request("GET", url)

        with zvmutils.expect_invalid_xcat_resp_data(res_dict):
            status = res_dict['node'][0][0]['data'][0]

        if status is not None:
            if status.__contains__('sshd'):
                return True

        return False

    def power_on(self, instance_name):
        """"Power on z/VM instance."""
        try:
            self._power_state(instance_name, "PUT", "on")
        except Exception as err:
            err_str = err.format_message()
            if ("Return Code: 200" in err_str and
                    "Reason Code: 8" in err_str):
                # Instance already not active
                LOG.warning("z/VM instance %s already active", instance_name)
                return

    def create_userid(self, instance_name, cpu, memory, image_name):
        """Create z/VM userid into user directory for a z/VM instance."""
        LOG.debug("Creating the z/VM user entry for instance %s"
                      % instance_name)

        kwprofile = 'profile=%s' % const.ZVM_USER_PROFILE
        body = [kwprofile,
                'password=%s' % CONF.zvm_user_default_password,
                'cpu=%i' % cpu,
                'memory=%im' % memory,
                'privilege=%s' % const.ZVM_USER_DEFAULT_PRIVILEGE,
                'ipl=%s' % CONF.zvm_user_root_vdev,
                'imagename=%s' % image_name]

        url = zvmutils.get_xcat_url().mkvm('/' + instance_name)

        try:
            zvmutils.xcat_request("POST", url, body)
            size = CONF.root_disk_units
            # Add root disk and set ipl
            self.add_mdisk(instance_name, CONF.zvm_diskpool,
                           CONF.zvm_user_root_vdev,
                               size)
            self.set_ipl(instance_name, CONF.zvm_user_root_vdev)

        except Exception as err:
            msg = ("Failed to create z/VM userid: %s") % err
            LOG.error(msg)
            raise ZVMException(msg=err)

    def add_mdisk(self, instance_name, diskpool, vdev, size, fmt=None):
        """Add a 3390 mdisk for a z/VM user.
    
        NOTE: No read, write and multi password specified, and
        access mode default as 'MR'.
    
        """
        disk_type = CONF.zvm_diskpool_type
        if (disk_type == 'ECKD'):
            action = '--add3390'
        elif (disk_type == 'FBA'):
            action = '--add9336'
        else:
            errmsg = ("Disk type %s is not supported.") % disk_type
            LOG.error(errmsg)
            raise ZVMException(msg=errmsg)
    
        if fmt:
            body = [" ".join([action, diskpool, vdev, size, "MR", "''", "''",
                    "''", fmt])]
        else:
            body = [" ".join([action, diskpool, vdev, size])]
        url = zvmutils.get_xcat_url().chvm('/' + instance_name)
        zvmutils.xcat_request("PUT", url, body)

    def set_ipl(self, instance_name, ipl_state):
        body = ["--setipl %s" % ipl_state]
        url = zvmutils.get_xcat_url().chvm('/' + instance_name)
        zvmutils.xcat_request("PUT", url, body)

    def instance_exists(self, instance_name):
        """Overwrite this to using instance name as input parameter."""
        return instance_name in self.list_instances()

    def list_instances(self):
        """Return the names of all the instances known to the virtualization
        layer, as a list.
        """
        zvm_host = CONF.zvm_host
        hcp_base = CONF.zhcp

        url = self._xcat_url.tabdump("/zvm")
        res_dict = zvmutils.xcat_request("GET", url)

        instances = []

        with zvmutils.expect_invalid_xcat_resp_data(res_dict):
            data_entries = res_dict['data'][0][1:]
            for data in data_entries:
                l = data.split(",")
                node, hcp = l[0].strip("\""), l[1].strip("\"")
                hcp_short = hcp_base.partition('.')[0]

                # zvm host and zhcp are not included in the list
                if (hcp.upper() == hcp_base.upper() and
                        node.upper() not in (zvm_host.upper(),
                        hcp_short.upper(), CONF.zvm_xcat_master.upper())):
                    instances.append(node)

        return instances

    def is_powered_off(self, instance_name):
        """Return True if the instance is powered off."""
        return self._check_power_stat(instance_name) == 'off'

    def _check_power_stat(self, instance_name):
        """Get power status of a z/VM instance."""
        LOG.debug('Query power stat of %s', instance_name)
        res_dict = self._power_state(instance_name,"GET", "stat")

        @zvmutils.wrap_invalid_xcat_resp_data_error
        def _get_power_string(d):
            tempstr = d['info'][0][0]
            return tempstr[(tempstr.find(':') + 2):].strip()

        power_stat = _get_power_string(res_dict)
        return power_stat

    def _delete_userid(self, url):
        try:
            zvmutils.xcat_request("DELETE", url)
        except Exception as err:
            emsg = err.format_message()
            LOG.debug("error emsg in delete_userid: %s", emsg)
            if (emsg.__contains__("Return Code: 400") and
                    emsg.__contains__("Reason Code: 4")):
                # zVM user definition not found, delete xCAT node directly
                self.delete_xcat_node()
            else:
                raise

    def delete_userid(self, instance_name, zhcp_node):
        """Delete z/VM userid for the instance.This will remove xCAT node
        at same time.
        """
        # Versions of xCAT that do not understand the instance ID and
        # request ID will silently ignore them.
        url = get_xcat_url().rmvm('/' + instance_name)

        try:
            self._delete_userid(url)
        except Exception as err:
            emsg = err.format_message()
            if (emsg.__contains__("Return Code: 400") and
               (emsg.__contains__("Reason Code: 16") or
                emsg.__contains__("Reason Code: 12"))):
                self._delete_userid(url)
            else:
                LOG.debug("exception not able to handle in delete_userid "
                          "%s", self._name)
                raise err
        except Exception as err:
            emsg = err.format_message()
            if (emsg.__contains__("Invalid nodes and/or groups") and
                    emsg.__contains__("Forbidden")):
                # Assume neither zVM userid nor xCAT node exist in this case
                return
            else:
                raise err

    def delete_xcat_node(self, instance_name):
        """Remove xCAT node for z/VM instance."""
        url = self._xcat_url.rmdef('/' + instance_name)
        try:
            zvmutils.xcat_request("DELETE", url)
        except Exception as err:
            if err.format_message().__contains__("Could not find an object"):
                # The xCAT node not exist
                return
            else:
                raise err

    def capture_instance(self, instance_name):
        """Invoke xCAT REST API to capture a instance."""
        LOG.info('Begin to capture instance %s' % instance_name)
        url = self._xcat_url.capture()
        nodename = instance_name
        image_id = str(uuid.uuid1())
        image_uuid = image_id.replace('-', '_')
        profile = image_uuid
        body = ['nodename=' + nodename,
                'profile=' + profile]
        res = zvmutils.xcat_request("POST", url, body)
        LOG.info(res['info'][3][0])
        image_name = res['info'][3][0].split('(')[1].split(')')[0]
        return image_name

    def delete_image(self, image_name):
        """"Invoke xCAT REST API to delete a image."""
        url = self._xcat_url.rmimage('/' + image_name)
        try:
            zvmutils.xcat_request("DELETE", url)
        except zvmutils.ZVMException:
            LOG.warn(("Failed to delete image file %s from xCAT") %
                    image_name)

        url = self._xcat_url.rmobject('/' + image_name)
        try:
            zvmutils.xcat_request("DELETE", url)
        except zvmutils.ZVMException:
            LOG.warn(("Failed to delete image definition %s from xCAT") %
                    image_name)
        LOG.info('Image %s successfully deleted' % image_name)


_VOLUMEOPS = None


def _get_volumeops():
    global _VOLUMEOPS
    if _VOLUMEOPS is None:
        _VOLUMEOPS = VOLUMEOps()
    return _VOLUMEOPS


class VOLUMEOps(object):

    def __init__(self):
        cwd = os.getcwd()
        self._zvm_volumes_file = os.path.join(cwd, const.ZVM_VOLUMES_FILE)
        if not os.path.exists(self._zvm_volumes_file):
            LOG.debug("z/VM volume management file %s does not exist, "
            "creating it." % self._zvm_volumes_file)
            try:
                os.mknod(self._zvm_volumes_file)
            except Exception as err:
                msg = ("Failed to create the z/VM volume management file, "
                       "error: %s" % str(err))
                raise zvmutils.ZVMException(msg)

    def _generate_vdev(self, base, offset=1):
        """Generate virtual device number based on base vdev.

        :param base: base virtual device number, string of 4 bit hex.
        :param offset: offset to base, integer.

        :output: virtual device number, string of 4 bit hex.
        """
        vdev = hex(int(base, 16) + offset)[2:]
        return vdev.rjust(4, '0')

    def get_free_mgr_vdev(self):
        """Get a free vdev address in volume_mgr userid

        Returns:
        :vdev:   virtual device number, string of 4 bit hex
        """
        vdev = CONF.volume_vdev_start
        if os.path.exists(self._zvm_volumes_file):
            volumes = []
            with open(self._zvm_volumes_file, 'r') as f:
                volumes = f.readlines()
            if len(volumes) >= 1:
                last_line = volumes[-1]
                last_vdev = last_line.strip().split(" ")[0]
                vdev = self._generate_vdev(last_vdev)
                LOG.debug("last_vdev used in volumes file: %s,"
                          " return vdev: %s", last_vdev, vdev)
            else:
                LOG.debug("volumes file has no vdev defined. ")
        else:
            msg = ("Cann't find z/VM volume management file")
            raise zvmutils.ZVMException(msg)
        return vdev

    def get_free_vdev(self, userid):
        """Get a free vdev address in target userid

        Returns:
        :vdev:   virtual device number, string of 4 bit hex
        """
        vdev = CONF.volume_vdev_start
        if os.path.exists(self._zvm_volumes_file):
            volumes = []
            with open(self._zvm_volumes_file, 'r') as f:
                volumes = f.readlines()
            max_vdev = ''
            for volume in volumes:
                volume_info = volume.strip().split(' ')
                attached_userid = volume_info[2]
                curr_vdev = volume_info[3]
                if (attached_userid == userid) and (
                    (max_vdev == '') or (
                        int(curr_vdev, 16) > int(max_vdev, 16))):
                    max_vdev = curr_vdev
            if max_vdev != '':
                vdev = self._generate_vdev(max_vdev)
                LOG.debug("max_vdev used in volumes file: %s,"
                              " return vdev: %s", max_vdev, vdev)
        else:
            msg = ("Cann't find z/VM volume management file")
            raise zvmutils.ZVMException(msg)
        LOG.debug("Final link address in target VM: %s", vdev)
        return vdev

    def get_volume_info(self, uuid):
        """Get the volume status from the volume management file

        Input parameters:
        :uuid: the uuid of the volume

        Returns a dict containing:
        :uuid:   the volume uuid, it's also the vdev in volume_mgr_userid
        :status: the status of the volume, one of the const.ZVM_VOLUME_STATUS
        :userid: the userid to which the volume belongs to
        :vdev:   the vdev of the volume in target vm
        """
        volume_info = {}
        if os.path.exists(self._zvm_volumes_file):
            volumes = []
            with open(self._zvm_volumes_file, 'r') as f:
                volumes = f.readlines()
            for volume in volumes:
                info = volume.strip().split(" ")
                if info[0] == uuid:
                    volume_info['uuid'] = info[0]
                    volume_info['status'] = info[1]
                    volume_info['userid'] = info[2]
                    volume_info['vdev'] = info[3]
                    break
        else:
            msg = ("Cann't find z/VM volume management file")
            raise zvmutils.ZVMException(msg)
        return volume_info

    def add_volume_info(self, volinfo):
        """Add one new volume in the z/VM volume management file

        Input parameters:
        a string containing the volume info string: uuid status userid vdev
        """
        if os.path.exists(self._zvm_volumes_file):
            with open(self._zvm_volumes_file, 'a') as f:
                f.write(volinfo + '\n')
        else:
            msg = ("Cann't find z/VM volume management file")
            raise zvmutils.ZVMException(msg)

    def delete_volume_info(self, uuid):
        """Delete the volume from the z/VM volume management file

        Input parameters:
        :uuid: uuid of the volume to be deleted
        """
        if os.path.exists(self._zvm_volumes_file):
            cmd = ("grep -i \"^%(uuid)s\" %(file)s") % {'uuid': uuid,
                   'file': self._zvm_volumes_file}
            status_lines = commands.getstatusoutput(cmd)[1].split("\n")
            if len(status_lines) != 1:
                msg = ("Found %(count) line status for volume %(uuid)s."
                       ) % {'count': len(status_lines), 'uuid': uuid}
                raise zvmutils.ZVMException(msg)
            # Delete the volume status line
            cmd = ("sed -i \'/^%(uuid)s.*/d\' %(file)s"
                   ) % {'uuid': uuid, 'file': self._zvm_volumes_file}
            LOG.debug("Deleting volume status, cmd: %s" % cmd)
            commands.getstatusoutput(cmd)
        else:
            msg = ("Cann't find z/VM volume management file")
            raise zvmutils.ZVMException(msg)

    def update_volume_info(self, volinfo):
        """Update volume info in the z/VM volume management file

        Input parameters:
        a string containing the volume info string: uuid status userid vdev
        """
        if os.path.exists(self._zvm_volumes_file):
            uuid = volinfo.split(' ')[0]
            # Check whether there are multiple lines correspond to this uuid
            cmd = ("grep -i \"^%(uuid)s\" %(file)s") % {'uuid': uuid,
                   'file': self._zvm_volumes_file}
            status_lines = commands.getstatusoutput(cmd)[1].split("\n")
            if len(status_lines) != 1:
                msg = ("Found %(count) line status for volume %(uuid)s."
                       ) % {'count': len(status_lines), 'uuid': uuid}
                raise zvmutils.ZVMException(msg)
            # Write the new status
            cmd = ("sed -i \'s/^%(uuid)s.*/%(new_line)s/g\' %(file)s"
                   ) % {'uuid': uuid, 'new_line': volinfo,
                   'file': self._zvm_volumes_file}
            LOG.debug("Updating volume status, cmd: %s" % cmd)
            commands.getstatusoutput(cmd)
        else:
            msg = ("Cann't find z/VM volume management file")
            raise zvmutils.ZVMException(msg)
