# -*- coding: utf-8 -*-
#
# Copyright (C) 2015 GNS3 Technologies Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
VMware player/workstation server module.
"""

import os
import sys
import re
import shutil
import asyncio
import subprocess
import logging
import codecs

from collections import OrderedDict
from gns3server.utils.interfaces import interfaces

log = logging.getLogger(__name__)

from ..base_manager import BaseManager
from .vmware_vm import VMwareVM
from .vmware_error import VMwareError


class VMware(BaseManager):

    _VM_CLASS = VMwareVM

    def __init__(self):

        super().__init__()
        self._execute_lock = asyncio.Lock()
        self._vmrun_path = None
        self._vmnets = []
        self._vmnet_start_range = 2
        if sys.platform.startswith("win"):
            self._vmnet_end_range = 19
        else:
            self._vmnet_end_range = 255

    @property
    def vmrun_path(self):
        """
        Returns the path vmrun utility.

        :returns: path
        """

        return self._vmrun_path

    @staticmethod
    def _find_vmrun_registry(regkey):

        import winreg
        try:
            # default path not used, let's look in the registry
            hkey = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, regkey)
            ws_install_path, _ = winreg.QueryValueEx(hkey, "InstallPath")
            vmrun_path = os.path.join(ws_install_path, "vmrun.exe")
            winreg.CloseKey(hkey)
            if os.path.exists(vmrun_path):
                return vmrun_path
        except OSError:
            pass
        return None

    def find_vmrun(self):
        """
        Searches for vmrun.

        :returns: path to vmrun
        """

        # look for vmrun
        vmrun_path = self.config.get_section_config("VMware").get("vmrun_path")
        if not vmrun_path:
            if sys.platform.startswith("win"):
                vmrun_path = shutil.which("vmrun")
                if vmrun_path is None:
                    # look for vmrun.exe in default VMware Workstation directory
                    vmrun_ws = os.path.expandvars(r"%PROGRAMFILES(X86)%\VMware\VMware Workstation\vmrun.exe")
                    if os.path.exists(vmrun_ws):
                        vmrun_path = vmrun_ws
                    else:
                        # look for vmrun.exe using the directory listed in the registry
                        vmrun_path = self._find_vmrun_registry(r"SOFTWARE\Wow6432Node\VMware, Inc.\VMware Workstation")
                    if vmrun_path is None:
                        # look for vmrun.exe in default VMware VIX directory
                        vmrun_vix = os.path.expandvars(r"%PROGRAMFILES(X86)%\VMware\VMware VIX\vmrun.exe")
                        if os.path.exists(vmrun_vix):
                            vmrun_path = vmrun_vix
                        else:
                            # look for vmrun.exe using the directory listed in the registry
                            vmrun_path = self._find_vmrun_registry(r"SOFTWARE\Wow6432Node\VMware, Inc.\VMware VIX")
            elif sys.platform.startswith("darwin"):
                vmrun_path = "/Applications/VMware Fusion.app/Contents/Library/vmrun"
            else:
                vmrun_path = shutil.which("vmrun")

        if not vmrun_path:
            raise VMwareError("Could not find vmrun")
        if not os.path.isfile(vmrun_path):
            raise VMwareError("vmrun {} is not accessible".format(vmrun_path))
        if not os.access(vmrun_path, os.X_OK):
            raise VMwareError("vmrun is not executable")
        if os.path.basename(vmrun_path) not in ["vmrun", "vmrun.exe"]:
            raise VMwareError("Invalid vmrun executable name {}".format(os.path.basename(vmrun_path)))

        self._vmrun_path = vmrun_path
        return vmrun_path

    @staticmethod
    def get_vmnet_interfaces():

        vmnet_interfaces = []
        for interface in interfaces():
            if sys.platform.startswith("win"):
                if "netcard" in interface:
                    windows_name = interface["netcard"]
                else:
                    windows_name = interface["name"]
                match = re.search("(VMnet[0-9]+)", windows_name)
                if match:
                    vmnet = match.group(1)
                    if vmnet not in ("VMnet1", "VMnet8"):
                        vmnet_interfaces.append(vmnet)
            elif interface["name"].startswith("vmnet"):
                vmnet = interface["name"]
                if vmnet not in ("vmnet1", "vmnet8"):
                    vmnet_interfaces.append(interface["name"])
        return vmnet_interfaces

    def is_managed_vmnet(self, vmnet):

        self._vmnet_start_range = self.config.get_section_config("VMware").getint("vmnet_start_range", self._vmnet_start_range)
        self._vmnet_end_range = self.config.get_section_config("VMware").getint("vmnet_end_range", self._vmnet_end_range)
        match = re.search("vmnet([0-9]+)$", vmnet, re.IGNORECASE)
        if match:
            vmnet_number = match.group(1)
            if self._vmnet_start_range <= int(vmnet_number) <= self._vmnet_end_range:
                return True
        return False

    def allocate_vmnet(self):

        if not self._vmnets:
            raise VMwareError("No more VMnet interfaces available")
        return self._vmnets.pop(0)

    def refresh_vmnet_list(self):

        vmnet_interfaces = self.get_vmnet_interfaces()

        # remove vmnets already in use
        for vm in self._vms.values():
            for used_vmnet in vm.vmnets:
                if used_vmnet in vmnet_interfaces:
                    log.debug("{} is already in use".format(used_vmnet))
                    vmnet_interfaces.remove(used_vmnet)

        # remove vmnets that are not managed
        for vmnet in vmnet_interfaces.copy():
            if vmnet in vmnet_interfaces and self.is_managed_vmnet(vmnet) is False:
                vmnet_interfaces.remove(vmnet)

        self._vmnets = vmnet_interfaces

    @property
    def host_type(self):
        """
        Returns the VMware host type.
        player = VMware player
        ws = VMware Workstation
        fusion = VMware Fusion

        :returns: host type (string)
        """

        if sys.platform.startswith("darwin"):
            host_type = "fusion"
        else:
            host_type = self.config.get_section_config("VMware").get("host_type", "ws")
        return host_type

    @asyncio.coroutine
    def execute(self, subcommand, args, timeout=60, host_type=None):

        with (yield from self._execute_lock):
            vmrun_path = self.vmrun_path
            if not vmrun_path:
                vmrun_path = self.find_vmrun()
            if host_type is None:
                host_type = self.host_type

            command = [vmrun_path, "-T", host_type, subcommand]
            command.extend(args)
            command_string = " ".join(command)
            log.info("Executing vmrun with command: {}".format(command_string))
            try:
                process = yield from asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            except (OSError, subprocess.SubprocessError) as e:
                raise VMwareError("Could not execute vmrun: {}".format(e))

            try:
                stdout_data, _ = yield from asyncio.wait_for(process.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                raise VMwareError("vmrun has timed out after {} seconds!".format(timeout))

            if process.returncode:
                # vmrun print errors on stdout
                vmrun_error = stdout_data.decode("utf-8", errors="ignore")
                raise VMwareError("vmrun has returned an error: {}".format(vmrun_error))

            return stdout_data.decode("utf-8", errors="ignore").splitlines()

    @staticmethod
    def parse_vmware_file(path):
        """
        Parses a VMware file (VMX, preferences or inventory).

        :param path: path to the VMware file

        :returns: dict
        """

        pairs = OrderedDict()
        encoding = "utf-8"
        # get the first line to read the .encoding value
        with open(path, "rb") as f:
            line = f.readline().decode(encoding, errors="ignore")
            if line.startswith("#!"):
                # skip the shebang
                line = f.readline().decode(encoding, errors="ignore")
            try:
                key, value = line.split('=', 1)
                if key.strip().lower() == ".encoding":
                    file_encoding = value.strip('" ')
                    try:
                        codecs.lookup(file_encoding)
                        encoding = file_encoding
                    except LookupError:
                        log.warning("Invalid file encoding detected in '{}': {}".format(path, file_encoding))
            except ValueError:
                log.warning("Couldn't find file encoding in {}, using {}...".format(path, encoding))

        # read the file with the correct encoding
        with open(path, encoding=encoding, errors="ignore") as f:
            for line in f.read().splitlines():
                try:
                    key, value = line.split('=', 1)
                    pairs[key.strip().lower()] = value.strip('" ')
                except ValueError:
                    continue
        return pairs

    @staticmethod
    def write_vmware_file(path, pairs):
        """
        Write a VMware file (excepting VMX file).

        :param path: path to the VMware file
        :param pairs: settings to write
        """

        encoding = "utf-8"
        if ".encoding" in pairs:
            file_encoding = pairs[".encoding"]
            try:
                codecs.lookup(file_encoding)
                encoding = file_encoding
            except LookupError:
                log.warning("Invalid file encoding detected in '{}': {}".format(path, file_encoding))
        with open(path, "w", encoding=encoding, errors="ignore") as f:
            for key, value in pairs.items():
                entry = '{} = "{}"\n'.format(key, value)
                f.write(entry)

    @staticmethod
    def write_vmx_file(path, pairs):
        """
        Write a VMware VMX file.

        :param path: path to the VMX file
        :param pairs: settings to write
        """

        encoding = "utf-8"
        if ".encoding" in pairs:
            file_encoding = pairs[".encoding"]
            try:
                codecs.lookup(file_encoding)
                encoding = file_encoding
            except LookupError:
                log.warning("Invalid file encoding detected in '{}': {}".format(path, file_encoding))
        with open(path, "w", encoding=encoding, errors="ignore") as f:
            if sys.platform.startswith("linux"):
                # write the shebang on the first line on Linux
                vmware_path = shutil.which("vmware")
                if vmware_path:
                    f.write("#!{}\n".format(vmware_path))
            for key, value in pairs.items():
                entry = '{} = "{}"\n'.format(key, value)
                f.write(entry)

    def _get_vms_from_inventory(self, inventory_path):
        """
        Searches for VMs by parsing a VMware inventory file.

        :param inventory_path: path to the inventory file

        :returns: list of VMs
        """

        vm_entries = {}
        vms = []
        try:
            log.debug('Reading VMware inventory file "{}"'.format(inventory_path))
            pairs = self.parse_vmware_file(inventory_path)
            for key, value in pairs.items():
                if key.startswith("vmlist"):
                    try:
                        vm_entry, variable_name = key.split('.', 1)
                    except ValueError:
                        continue
                    if vm_entry not in vm_entries:
                        vm_entries[vm_entry] = {}
                    vm_entries[vm_entry][variable_name.strip()] = value
        except OSError as e:
            log.warning("Could not read VMware inventory file {}: {}".format(inventory_path, e))

        for vm_settings in vm_entries.values():
            if "displayname" in vm_settings and "config" in vm_settings:
                log.debug('Found VM named "{}" with VMX file "{}"'.format(vm_settings["displayname"], vm_settings["config"]))
                vms.append({"vmname": vm_settings["displayname"], "vmx_path": vm_settings["config"]})
        return vms

    def _get_vms_from_directory(self, directory):
        """
        Searches for VMs in a given directory.

        :param directory: path to the directory

        :returns: list of VMs
        """

        vms = []
        for path, _, filenames in os.walk(directory):
            for filename in filenames:
                if os.path.splitext(filename)[1] == ".vmx":
                    vmx_path = os.path.join(path, filename)
                    log.debug('Reading VMware VMX file "{}"'.format(vmx_path))
                    try:
                        pairs = self.parse_vmware_file(vmx_path)
                        if "displayname" in pairs:
                            log.debug('Found VM named "{}"'.format(pairs["displayname"]))
                            vms.append({"vmname": pairs["displayname"], "vmx_path": vmx_path})
                    except OSError as e:
                        log.warning('Could not read VMware VMX file "{}": {}'.format(vmx_path, e))
                        continue
        return vms

    @staticmethod
    def get_vmware_inventory_path():
        """
        Returns VMware inventory file path.

        :returns: path to the inventory file
        """

        if sys.platform.startswith("win"):
            return os.path.expandvars(r"%APPDATA%\Vmware\Inventory.vmls")
        elif sys.platform.startswith("darwin"):
            return os.path.expanduser("~/Library/Application\ Support/VMware Fusion/vmInventory")
        else:
            return os.path.expanduser("~/.vmware/inventory.vmls")

    @staticmethod
    def get_vmware_preferences_path():
        """
        Returns VMware preferences file path.

        :returns: path to the preferences file
        """

        if sys.platform.startswith("win"):
            return os.path.expandvars(r"%APPDATA%\VMware\preferences.ini")
        elif sys.platform.startswith("darwin"):
            return os.path.expanduser("~/Library/Preferences/VMware Fusion/preferences")
        else:
            return os.path.expanduser("~/.vmware/preferences")

    @staticmethod
    def get_vmware_default_vm_path():
        """
        Returns VMware default VM directory path.

        :returns: path to the default VM directory
        """

        if sys.platform.startswith("win"):
            return os.path.expandvars(r"%USERPROFILE%\Documents\Virtual Machines")
        elif sys.platform.startswith("darwin"):
            return os.path.expanduser("~/Documents/Virtual Machines.localized")
        else:
            return os.path.expanduser("~/vmware")

    def list_vms(self):
        """
        Gets VMware VM list.
        """

        inventory_path = self.get_vmware_inventory_path()
        if os.path.exists(inventory_path):
            return self._get_vms_from_inventory(inventory_path)
        else:
            # VMware player has no inventory file, let's search the default location for VMs.
            vmware_preferences_path = self.get_vmware_preferences_path()
            default_vm_path = self.get_vmware_default_vm_path()

            if os.path.exists(vmware_preferences_path):
                # the default vm path may be present in VMware preferences file.
                try:
                    pairs = self.parse_vmware_file(vmware_preferences_path)
                    if "prefvmx.defaultvmpath" in pairs:
                        default_vm_path = pairs["prefvmx.defaultvmpath"]
                except OSError as e:
                    log.warning('Could not read VMware preferences file "{}": {}'.format(vmware_preferences_path, e))

            if not os.path.isdir(default_vm_path):
                raise VMwareError('Could not find the default VM directory: "{}"'.format(default_vm_path))
            return self._get_vms_from_directory(default_vm_path)
