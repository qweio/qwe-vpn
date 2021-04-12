import fileinput
import os
import re
import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

import netifaces

import src.utils.logger as logger
from src.executor.shell_executor import SystemHelper
from src.utils.helper import FileHelper, grep
from src.utils.opts_shared import UnixServiceOpts


class ServiceStatus(Enum):
    RUNNING = 'active(running)'
    EXITED = 'active(exited)'
    WAITING = 'active(waiting)'
    INACTIVE = 'inactive(dead)'
    UNKNOWN = 'unknown'

    @staticmethod
    def parse(status: str):
        status_ = [e for e in list(ServiceStatus) if e.value == status]
        return status_[0] if len(status_) else ServiceStatus.UNKNOWN


class UnixServiceType(Enum):
    SYSTEMD = 'systemd'
    PROCD = 'procd'


class IPResolverType(Enum):
    DHCLIENT = 'dhclient'
    UDHCPC = 'udhcpc'


class DHCPReason(Enum):
    """
    https://linux.die.net/man/8/dhclient-script
    """
    MEDIUM = 1
    PREINIT = 2
    BOUND = 3
    RENEW = 4
    REBIND = 5
    REBOOT = 6
    EXPIRE = 7
    FAIL = 8
    STOP = 9
    RELEASE = 10
    NBI = 11
    TIMEOUT = 12


class DNSResolverType(Enum):
    CONNMAN = 'connman'
    DNSMASQ = 'dnsmasq'
    SYSTEMD_RESOLVED = 'systemd-resolved'
    NETWORK_MANAGER = 'NetworkManager'
    RESOLVCONF = 'resolvconf'
    UNKNOWN = 'unknown'


class UnixService(ABC):

    @staticmethod
    @abstractmethod
    def factory() -> 'UnixService':
        pass

    @property
    @abstractmethod
    def kind(self) -> UnixServiceType:
        pass

    @abstractmethod
    def create(self, opts: UnixServiceOpts, service_tmpl_path: str, replacements: dict):
        pass

    @abstractmethod
    def disable(self, opts: UnixServiceOpts, force: bool = False):
        pass

    @abstractmethod
    def restart(self, service_name: str, delay: int = 1):
        pass

    @abstractmethod
    def status(self, service_name: str) -> ServiceStatus:
        pass

    @abstractmethod
    def stop(self, service_name: str):
        pass

    @abstractmethod
    def to_service_fqn(self, service_dir: str, service_name: str):
        pass


class DNSResolver:
    DNS_SYSTEM_FILE = '/etc/resolv.conf'

    def __init__(self, cache_dir: str, unix_service: UnixService):
        self.cache_dir = cache_dir
        self.dns_origin_cfg = os.path.join(self.cache_dir, 'resolv.origin.conf')
        self.dns_vpn_cfg = os.path.join(self.cache_dir, 'resolv.vpn.conf')
        self.service = unix_service
        self.kind = None

    def probe(self) -> 'DNSResolver':
        self.kind = next((t for t in DNSResolverType if self.service.status(t.value) is ServiceStatus.RUNNING), None)
        if not self.kind and SystemHelper.verify_command('resolvconf'):
            self.kind = DNSResolverType.RESOLVCONF
        if not self.kind:
            raise NotImplementedError('Unknown DNS resolver')
        return self

    def resolve(self, nic: str):
        if self.kind == DNSResolverType.CONNMAN:
            self.__tweak_connman(nic)
        if self.kind == DNSResolverType.DNSMASQ:
            self.__tweak_dnsmasq(nic)
        if self.kind == DNSResolverType.SYSTEMD_RESOLVED:
            self.__tweak_systemd_resolved(nic)
        if self.kind == DNSResolverType.NETWORK_MANAGER:
            self.__tweak_network_manager(nic)
        if self.kind == DNSResolverType.RESOLVCONF:
            self.__tweak_resolvconf(nic)

    def __tweak_connman(self, nic):
        restart = False
        with fileinput.FileInput("/etc/connman/main.conf", inplace=True, backup='.bak') as f:
            for line in f:
                if re.match(r"^NetworkInterfaceBlacklist\s*=\s*", line, re.IGNORECASE) and nic not in line:
                    restart = True
                    print(line.strip() + "," + nic)
                else:
                    print(line, end='')
        if restart:
            self.service.restart("connman")

    def __tweak_systemd_resolved(self, nic):
        pass

    def __tweak_network_manager(self, nic):
        pass

    def __tweak_dnsmasq(self, nic):
        pass

    def __tweak_resolvconf(self, nic):
        pass

    def tweak(self, reason: DHCPReason, new_name_servers: str, old_name_servers: str):
        if reason in [DHCPReason.PREINIT] or (reason == DHCPReason.RENEW and new_name_servers == old_name_servers):
            logger.info(f'Skip generating DNS entry in [{reason.name}]')
            return
        nameservers = [f'nameserver {ns}' for ns in new_name_servers.split(',')[0:2] if ns]
        if not FileHelper.is_file_readable(self.dns_origin_cfg):
            FileHelper.backup(DNSResolver.DNS_SYSTEM_FILE, self.dns_origin_cfg, remove=False)
        origins = grep(FileHelper.read_file_by_line(self.dns_origin_cfg), r'nameserver .+')
        nameservers += origins[0:(3 - len(nameservers))]
        content = '\n'.join(nameservers)
        logger.info(f'Generate VPN DNS config file in [{reason.name}] with nameservers {nameservers}...')
        FileHelper.write_file(self.dns_vpn_cfg, f'### Generated by VPN service\n{content}\n', mode=0o0644)
        FileHelper.create_symlink(self.dns_vpn_cfg, DNSResolver.DNS_SYSTEM_FILE, force=True)
        logger.info(FileHelper.read_file_by_line(DNSResolver.DNS_SYSTEM_FILE))

    def rollback_origin(self):
        logger.info(f'Restore system DNS config file...')
        if not FileHelper.is_file_readable(self.dns_origin_cfg):
            return
        FileHelper.backup(self.dns_origin_cfg, DNSResolver.DNS_SYSTEM_FILE)


class IPResolver(ABC):

    @staticmethod
    @abstractmethod
    def factory(cache_dir: str, log_lvl: int, silent: bool = True) -> 'IPResolver':
        pass

    def __init__(self, cache_dir: str, log_lvl: int, silent: bool = True):
        self.cache_dir = cache_dir
        self.log_lvl = log_lvl
        self.silent = silent

    @property
    @abstractmethod
    def ip_tool(self) -> str:
        pass

    @property
    def pid_file(self):
        return os.path.join(self.cache_dir, 'vpn_dhclient.pid')

    @property
    def lease_file(self):
        return os.path.join(self.cache_dir, 'vpn_dhclient.lease')

    @abstractmethod
    def add_hook(self, hook_tmpl_path: str, service_name: str, replacements: dict):
        pass

    @abstractmethod
    def remove_hook(self, service_name: str):
        pass

    @abstractmethod
    def create_config(self, config_tmpl_path: str, vpn_acc: str, replacements: dict):
        pass

    def lease_ip(self, vpn_acc: str, vpn_nic: str, daemon=True):
        logger.log(self.log_lvl, 'Lease a new VPN IP...')
        SystemHelper.exec_command(f'{self.ip_tool} {self._lease_ip_opt(vpn_acc, vpn_nic, daemon)}',
                                  silent=self.silent, log_lvl=self.log_lvl)

    def release_ip(self, vpn_acc: str, vpn_nic: str):
        logger.log(self.log_lvl, 'Release the current VPN IP...')
        SystemHelper.exec_command(f'{self.ip_tool} {self._release_ip_opt(vpn_acc, vpn_nic)}',
                                  silent=self.silent, log_lvl=self.log_lvl)

    def renew_all_ip(self, delay=1):
        logger.log(self.log_lvl, 'Refresh all IPs...')
        time.sleep(delay)
        SystemHelper.exec_command(f'{self._refresh_all_ip_opt()}', silent=self.silent,
                                  log_lvl=logger.down_lvl(self.log_lvl))

    def cleanup_vpn_ip(self):
        logger.log(self.log_lvl, 'Cleanup all ip lease process for VPN...')
        SystemHelper.ps_kill(f'{self.ip_tool} .* vpn_', silent=self.silent, log_lvl=logger.down_lvl(self.log_lvl))

    def get_vpn_ip(self, nic: str):
        try:
            return netifaces.ifaddresses(nic)[netifaces.AF_INET]
        except Exception as err:
            logger.warn(f'Not found VPN IP {nic}. Error: {err}')
            return None

    def _to_config_file(self, suffix):
        return os.path.join(self.cache_dir, f'vpn_dhclient.{suffix}.conf')

    @abstractmethod
    def _lease_ip_opt(self, vpn_acc: str, vpn_nic: str, daemon=True) -> str:
        pass

    @abstractmethod
    def _release_ip_opt(self, vpn_acc: str, vpn_nic: str) -> str:
        pass

    @abstractmethod
    def _refresh_all_ip_opt(self) -> str:
        pass

    @abstractmethod
    def _to_hook_file(self, service_name: str) -> str:
        pass


class SystemdService(UnixService):
    """
    Systemd
    """

    @staticmethod
    def factory() -> 'UnixService':
        if SystemHelper.verify_command(f'pidof {UnixServiceType.SYSTEMD.value}'):
            return SystemdService()
        return None

    @property
    def kind(self) -> UnixServiceType:
        return UnixServiceType.SYSTEMD

    def create(self, opts: UnixServiceOpts, service_tmpl_path: str, replacements: dict):
        service_fqn = self.to_service_fqn(opts.service_dir, opts.service_name)
        FileHelper.copy(service_tmpl_path, service_fqn, force=True)
        FileHelper.replace_in_file(service_fqn, replacements, backup='')
        FileHelper.chmod(service_fqn, mode=0o0644)
        logger.info(f"Enable System service '{opts.service_name}[{service_fqn}]'...", )
        SystemHelper.exec_command(f"systemctl enable {opts.service_name}", log_lvl=logger.INFO)

    def disable(self, opts: UnixServiceOpts, force: bool = False):
        service_fqn = self.to_service_fqn(opts.service_dir, opts.service_name)
        logger.info(f"Disable System service '{opts.service_name}'...")
        SystemHelper.exec_command(f"systemctl stop {opts.service_name}", silent=True, log_lvl=logger.INFO)
        SystemHelper.exec_command(f"systemctl disable {opts.service_name}", silent=True, log_lvl=logger.INFO)
        if force and os.path.exists(service_fqn):
            logger.info("Remove System service '%s'...", opts.service_name)
            os.remove(service_fqn)
        SystemHelper.exec_command("systemctl daemon-reload", silent=True, log_lvl=logger.INFO)

    def restart(self, service_name, delay: int = 1):
        logger.info(f"Restart System service '{service_name}'...")
        SystemHelper.exec_command(f"systemctl restart {service_name}", log_lvl=logger.INFO)
        time.sleep(delay)

    def status(self, service_name: str) -> ServiceStatus:
        status = SystemHelper.exec_command(f"systemctl status {service_name} | grep Active | awk '{{print $2$3}}'",
                                           shell=True, silent=True, log_lvl=logger.DEBUG)
        return ServiceStatus.parse(status)

    def stop(self, service_name):
        logger.info(f"Stop System service '{service_name}'...")
        SystemHelper.exec_command(f"systemctl stop {service_name}", silent=True, log_lvl=logger.INFO)

    def to_service_fqn(self, service_dir: str, service_name: str):
        return os.path.join(service_dir or '/lib/systemd/system', service_name + '.service')


class ProcdService(UnixService, ABC):
    """
    Procd for OpenWRT: https://openwrt.org/docs/techref/procd
    """

    @staticmethod
    def factory() -> 'UnixService':
        if SystemHelper.verify_command(f'pidof {UnixServiceType.PROCD.value}'):
            raise NotImplementedError('Not yet supported OpenWRT')
        return None


class DHCPResolver(IPResolver):

    @staticmethod
    def factory(cache_dir: str, log_lvl: int, silent: bool = True) -> 'IPResolver':
        if FileHelper.which(IPResolverType.DHCLIENT.value):
            return DHCPResolver(cache_dir, log_lvl, silent)
        return None

    @property
    def ip_tool(self) -> str:
        return IPResolverType.DHCLIENT.value

    def create_config(self, config_tmpl_path: str, vpn_acc: str, replacements: dict):
        config_file = self._to_config_file(vpn_acc)
        logger.log(self.log_lvl, f'Create DHCP client VPN config[{config_file}]...')
        FileHelper.copy(config_tmpl_path, config_file, force=True)
        FileHelper.replace_in_file(config_file, replacements, backup='')
        FileHelper.chmod(config_file, mode=0o0644)

    def add_hook(self, hook_tmpl_path: str, service_name: str, replacements: dict):
        hook_file = self._to_hook_file(service_name)
        logger.log(self.log_lvl, f'Create DHCP client VPN hook[{hook_file}]...')
        FileHelper.copy(hook_tmpl_path, hook_file, force=True)
        FileHelper.replace_in_file(hook_file, replacements, backup='')
        FileHelper.chmod(hook_file, mode=0o0744)

    def remove_hook(self, service_name: str):
        hook_file = self._to_hook_file(service_name)
        logger.log(self.log_lvl, f'Remove DHCP client VPN hook[{hook_file}]...')
        FileHelper.remove_files(hook_file, force=True)

    def _to_hook_file(self, service_name: str) -> str:
        return os.path.join('/etc/dhcp/dhclient-exit-hooks.d', service_name)

    def _lease_ip_opt(self, vpn_acc: str, vpn_nic: str, daemon=True) -> str:
        opts = f'-nw' if daemon else '-1'
        opts += f' -lf {self.lease_file} -pf {self.pid_file} -v'
        # opts += f' -cf {self._to_config_file(vpn_acc)}'
        return f'{opts} {vpn_nic}'

    def _release_ip_opt(self, vpn_acc: str, vpn_nic: str) -> str:
        opts = '-r'
        opts += f' -lf {self.lease_file} -pf {self.pid_file} -v'
        # opts += f' -cf {self._to_config_file(vpn_acc)}'
        return f'{opts} {vpn_nic}'

    def _refresh_all_ip_opt(self):
        return f'{self.ip_tool} -1 -v'


class UDHCPCResolver(IPResolver, ABC):

    @property
    def ip_tool(self) -> str:
        return IPResolverType.UDHCPC.value


class DeviceResolver:

    def __init__(self, cache_dir: str, log_lvl=logger.DEBUG, silent=True):
        self.cache_dir = cache_dir
        self.log_lvl = log_lvl
        self.silent = silent
        self.__service = None
        self.__ip_resolver = None
        self.__dns_resolver = None

    def probe(self) -> 'DeviceResolver':
        self._service(SystemdService.factory() or ProcdService.factory())
        self._ip_resolver(DHCPResolver.factory(self.cache_dir, self.log_lvl, self.silent))
        self._dns_resolver(DNSResolver(self.cache_dir, self.unix_service).probe())
        return self

    @property
    def unix_service(self) -> UnixService:
        return self.__service

    @property
    def ip_resolver(self) -> IPResolver:
        return self.__ip_resolver

    @property
    def dns_resolver(self) -> DNSResolver:
        return self.__dns_resolver

    def _service(self, service: UnixService):
        self.__service = self.__not_null(service, 'INIT system')

    def _ip_resolver(self, resolver: IPResolver):
        self.__ip_resolver = self.__not_null(resolver, 'IP resolver')

    def _dns_resolver(self, resolver: DNSResolver):
        self.__dns_resolver = self.__not_null(resolver, 'DNS resolver')

    def __not_null(self, obj: Any, msg: str):
        if not obj:
            raise NotImplementedError(f'Unknown {msg}')
        return obj
