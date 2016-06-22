# OpenStackCharm() - base class for build OpenStack charms from for the
# reactive framework.

# need/want absolute imports for the package imports to work properly
from __future__ import absolute_import

import base64
import os
import random
import string
import subprocess
import contextlib
import collections

import six

import charmhelpers.contrib.network.ip as ch_ip
import charmhelpers.contrib.openstack.templating as os_templating
import charmhelpers.contrib.openstack.utils as os_utils
import charmhelpers.core.hookenv as hookenv
import charmhelpers.core.host as ch_host
import charmhelpers.core.templating
import charmhelpers.fetch
import charms.reactive.bus

import charms_openstack.ip as os_ip
import charms_openstack.adapters as os_adapters


# _releases{} is a dictionary of release -> class that is instantiated
# according to the the release that is being requested.  i.e. a charm can
# handle more than one release.  The OpenStackCharm() derived class sets the
# `release` variable to indicate which release that the charm supports.
# Any subsequent releases that need a different/specialised charm uses the
# `release` class property to indicate that it handles that release onwards.
_releases = {}

# `_singleton` stores the instance of the class that is being used during a
# hook invocation.
_singleton = None

# List of releases that OpenStackCharm based charms know about
KNOWN_RELEASES = [
    'diablo',
    'essex',
    'folsom',
    'grizzly',
    'havana',
    'icehouse',
    'juno',
    'kilo',
    'liberty',
    'mitaka',
]

VIP_KEY = "vip"
CIDR_KEY = "vip_cidr"
IFACE_KEY = "vip_iface"


def get_charm_instance(release=None, *args, **kwargs):
    """Get an instance of the charm based on the release (or use the
    default if release is None).

    OS releases are in alphabetical order, so it looks for the first release
    that is provided if release is None, otherwise it finds the release that is
    before or equal to the release passed.

    Note that it passes args and kwargs to the class __init__() method.

    :param release: lc string representing release wanted.
    :returns: OpenStackCharm() derived class according to cls.releases
    """
    if len(_releases.keys()) == 0:
        raise RuntimeError("No derived OpenStackCharm() classes registered")
    # Note that this relies on OS releases being in alphabetica order
    known_releases = sorted(_releases.keys())
    cls = None
    if release is None:
        # take the latest version of the charm if no release is passed.
        cls = _releases[known_releases[-1]]
    elif release < known_releases[0]:
        raise RuntimeError(
            "Release {} is not supported by this charm. Earliest support is "
            "{} release".format(release, known_releases[0]))
    else:
        # try to find the release that is supported.
        for known_release in reversed(known_releases):
            if release >= known_release:
                cls = _releases[known_release]
                break
    if cls is None:
        raise RuntimeError("Release {} is not supported".format(release))
    return cls(release=release, *args, **kwargs)


class OpenStackCharmMeta(type):
    """Metaclass to provide a classproperty of 'singleton' so that class
    methods in the derived OpenStackCharm() class can simply use cls.singleton
    to get the instance of the charm.

    Thus cls.singleton is a singleton for accessing and creating the default
    OpenStackCharm() derived class.  This is to avoid a lot of boilerplate in
    the classmethods for the charm code.  This is because, usually, a
    classmethod is only called once per invocation of the script.

    Thus in the derived charm code we can do this:

        cls.singleton.instance_method(...)

    and this will instatiate the charm and call instance_method() on it.

    Note that self.singleton is also defined as a property for completeness so
    that cls.singleton and self.singleton give consistent results.
    """

    def __init__(cls, name, mro, members):
        """Receive the OpenStackCharm() (derived) class and store the release
        that it works against.  Each class defines a 'release' that it handles
        and the order of releases (as given in charmhelpers) determines (for
        any release) which OpenStackCharm() derived class is the handler for
        that class.  Note, that if the `name` is 'OpenStackCharm' then the
        function ignores the release, etc.

        :param name: string for class name.
        :param mro: tuple of base classes.
        :param members: dictionary of name to class attribute (f, p, a, etc.)
        """
        global _releases
        # Do not attempt to calculate the release for an abstract class
        if members.get('abstract_class', False):
            return
        if 'release' in members.keys():
            release = members['release']
            if release not in KNOWN_RELEASES:
                raise RuntimeError(
                    "Release {} is not a known OpenStack release"
                    .format(release))
            if release in _releases.keys():
                raise RuntimeError(
                    "Release {} defined more than once in classes {} and {} "
                    " (at least)"
                    .format(release, _releases[release].__name__, name))
            # store the class against the release.
            _releases[release] = cls
        else:
            raise RuntimeError(
                "class '{}' does not define a release that it supports. "
                "Please use the 'release' class property to define the "
                "release.".format(name))

    @property
    def singleton(cls):
        global _singleton
        if _singleton is None:
            _singleton = get_charm_instance()
        return _singleton


@six.add_metaclass(OpenStackCharmMeta)
class OpenStackCharm(object):
    """
    Base class for all OpenStack Charm classes;
    encapulates general OpenStack charm payload operations

    Theory:
    Derive form this class, set the name, first_release and releases class
    variables so that get_charm_instance() will create an instance of this
    charm.

    See the other class variables for details on what they are for and do.
    """

    abstract_class = True

    # first_release = this is the first release in which this charm works
    release = 'icehouse'

    # The name of the charm (for printing, etc.)
    name = 'charmname'

    # List of packages to install
    packages = []

    # Dictionary mapping services to ports for public, admin and
    # internal endpoints
    api_ports = {}

    # Keystone endpoint type
    service_type = None

    # Default service for the charm
    default_service = None

    # A dictionary of:
    # {
    #    'config.file': ['list', 'of', 'services', 'to', 'restart'],
    #    'config2.file': ['more', 'services'],
    # }
    restart_map = {}

    # The command used to sync the database
    sync_cmd = []

    # The list of services that this charm manages
    services = []

    # The adapters class that this charm uses to adapt interfaces.
    adapters_class = None

    ha_resources = []
    adapters_class = None
    HAPROXY_CONF = '/etc/haproxy/haproxy.cfg'

    @property
    def singleton(self):
        """Return the only instance of the charm class in this run"""
        # Note refers back to the Metaclass property for this charm.
        return self.__class__.singleton

    def __init__(self, interfaces=None, config=None, release=None):
        """Instantiate an instance of the class.

        Sets up self.config and self.adapter_instance if cls.adapters_class and
        interfaces has been set.

        :param interfaces: list of interface instances for the charm.
        :param config: the config for the charm (optionally None for
        automatically using config())
        """
        self.config = config or hookenv.config()
        self.release = release
        self.adapters_instance = None
        if interfaces and self.adapters_class:
            self.adapters_instance = self.adapters_class(interfaces)

    @property
    def all_packages(self):
        """List of packages to be installed

        @return ['pkg1', 'pkg2', ...]
        """
        return self.packages

    @property
    def full_restart_map(self):
        """Map of services to be restarted if a file changes

        @return {
                    'file1': ['svc1', 'svc3'],
                    'file2': ['svc2', 'svc3'],
                    ...
                }
        """
        return self.restart_map

    def install(self):
        """Install packages related to this charm based on
        contents of self.packages attribute.
        """
        packages = charmhelpers.fetch.filter_installed_packages(
            self.all_packages)
        if packages:
            hookenv.status_set('maintenance', 'Installing packages')
            charmhelpers.fetch.apt_install(packages, fatal=True)
            # TODO need a call to assess_status(...) or equivalent so that we
            # can determine the workload status at the end of the handler.  At
            # the end of install the 'status' is stuck in maintenance until the
            # next hook is run.
        self.set_state('{}-installed'.format(self.name))

    def set_state(self, state, value=None):
        """proxy for charms.reactive.bus.set_state()"""
        charms.reactive.bus.set_state(state, value)

    def remove_state(self, state):
        """proxy for charms.reactive.bus.remove_state()"""
        charms.reactive.bus.remove_state(state)

    def api_port(self, service, endpoint_type=os_ip.PUBLIC):
        """Return the API port for a particular endpoint type from the
        self.api_ports{}.

        :param service: string for service name
        :param endpoing_type: one of charm.openstack.ip.PUBLIC| INTERNAL| ADMIN
        :returns: port (int)
        """
        return self.api_ports[service][endpoint_type]

    def configure_source(self):
        """Configure installation source using the config item
        'openstack-origin'

        This configures the installation source for deb packages and then
        updates the packages list on the unit.
        """
        os_utils.configure_installation_source(self.config['openstack-origin'])
        charmhelpers.fetch.apt_update(fatal=True)

    @property
    def region(self):
        """Return the OpenStack Region as contained in the config item 'region'
        """
        return self.config['region']

    @property
    def public_url(self):
        """Return the public endpoint URL for the default service as specified
        in the self.default_service attribute
        """
        return "{}:{}".format(os_ip.canonical_url(os_ip.PUBLIC),
                              self.api_port(self.default_service,
                                            os_ip.PUBLIC))

    @property
    def admin_url(self):
        """Return the admin endpoint URL for the default service as specificed
        in the self.default_service attribute
        """
        return "{}:{}".format(os_ip.canonical_url(os_ip.ADMIN),
                              self.api_port(self.default_service,
                                            os_ip.ADMIN))

    @property
    def internal_url(self):
        """Return the internal internal endpoint URL for the default service as
        specificated in the self.default_service attribtue
        """
        return "{}:{}".format(os_ip.canonical_url(os_ip.INTERNAL),
                              self.api_port(self.default_service,
                                            os_ip.INTERNAL))

    @contextlib.contextmanager
    def restart_on_change(self):
        """Restart the services in the self.restart_map{} attribute if any of
        the files identified by the keys changes for the wrapped call.

        This function is a @decorator that checks if the wrapped function
        changes any of the files identified by the keys in the
        self.restart_map{} and, if they change, restarts the services in the
        corresponding list.
        """
        checksums = {path: ch_host.path_hash(path)
                     for path in self.full_restart_map.keys()}
        yield
        restarts = []
        for path in self.full_restart_map:
            if ch_host.path_hash(path) != checksums[path]:
                restarts += self.full_restart_map[path]
        services_list = list(collections.OrderedDict.fromkeys(restarts).keys())
        for service_name in services_list:
            ch_host.service_restart(service_name)

    def render_all_configs(self, adapters_instance=None):
        """Render (write) all of the config files identified as the keys in the
        self.restart_map{}

        Note: If the config file changes on storage as a result of the config
        file being written, then the services are restarted as per
        the restart_the_services() method.

        If adapters_instance is None then the self.adapters_instance is used
        that was setup in the __init__() method.

        :param adapters_instance: [optional] the adapters_instance to use.
        """
        self.render_configs(self.full_restart_map.keys(),
                            adapters_instance=adapters_instance)

    def render_configs(self, configs, adapters_instance=None):
        """Render the configuration files identified in the list passed as
        configs.

        If adapters_instance is None then the self.adapters_instance is used
        that was setup in the __init__() method.

        :param configs: list of strings, the names of the configuration files.
        :param adapters_instance: [optional] the adapters_instance to use.
        """
        if adapters_instance is None:
            adapters_instance = self.adapters_instance
        with self.restart_on_change():
            for conf in configs:
                charmhelpers.core.templating.render(
                    source=os.path.basename(conf),
                    template_loader=os_templating.get_loader(
                        'templates/', self.release),
                    target=conf,
                    context=adapters_instance)

    def render_with_interfaces(self, interfaces, configs=None):
        """Render the configs using the interfaces passed; overrides any
        interfaces passed in the instance creation.

        :param interfaces: list of interface objects to render against
        """
        if not configs:
            configs = self.full_restart_map.keys()
        self.render_configs(
            configs,
            adapters_instance=self.adapters_class(interfaces))

    def restart_all(self):
        """Restart all the services configured in the self.services[]
        attribute.
        """
        for svc in self.services:
            ch_host.service_restart(svc)

    def db_sync_done(self):
        return hookenv.leader_get(attribute='db-sync-done')

    def db_sync(self):
        """Perform a database sync using the command defined in the
        self.sync_cmd attribute. The services defined in self.services are
        restarted after the database sync.
        """
        if not self.db_sync_done() and hookenv.is_leader():
            subprocess.check_call(self.sync_cmd)
            hookenv.leader_set({'db-sync-done': True})


class HAOpenStackCharm(OpenStackCharm):

    abstract_class = True

    def __init__(self, **kwargs):
        super(HAOpenStackCharm, self).__init__(**kwargs)
        self.set_haproxy_stat_password()
        self.set_config_defined_certs_and_keys()
        self.enable_apache()

    @property
    def apache_vhost_file(self):
        return '/etc/apache2/sites-available/openstack_https_frontend.conf'

    def enable_apache(self):
        if os.path.exists(self.apache_vhost_file):
            check_enabled = subprocess.call(
                ['a2query', '-s', 'openstack_https_frontend'])
            if check_enabled != 0:
                subprocess.check_call(['a2ensite', 'openstack_https_frontend'])

    @property
    def all_packages(self):
        """List of packages to be installed

        @return ['pkg1', 'pkg2', ...]
        """
        _packages = self.packages[:]
        if self.haproxy_enabled():
            _packages.append('haproxy')
        if self.apache_enabled():
            _packages.append('apache2')
        return _packages

    @property
    def full_restart_map(self):
        """Map of services to be restarted if a file changes

        @return {
                    'file1': ['svc1', 'svc3'],
                    'file2': ['svc2', 'svc3'],
                    ...
                }
        """
        _restart_map = self.restart_map.copy()
        if self.haproxy_enabled():
            _restart_map[self.HAPROXY_CONF] = ['haproxy']
        if self.apache_enabled():
            _restart_map[self.apache_vhost_file] = ['apache2']
        return _restart_map

    def apache_enabled(self):
        """Determine if apache is being used

        @return True if apache is being used"""
        return charms.reactive.bus.get_state('ssl.enabled')

    def haproxy_enabled(self):
        """Determine if haproxy is fronting the services

        @return True if haproxy is fronting the service"""
        return 'haproxy' in self.ha_resources

    def configure_ha_resources(self, hacluster):
        """Inform the ha subordinate about each service it should manage. The
        child class specifies the services via self.ha_resources

        @param hacluster instance of interface class HAClusterRequires
        """
        RESOURCE_TYPES = {
            'vips': self._add_ha_vips_config,
            'haproxy': self._add_ha_haproxy_config,
        }
        if self.ha_resources:
            for res_type in self.ha_resources:
                RESOURCE_TYPES[res_type](hacluster)
            hacluster.bind_resources(iface=self.config[IFACE_KEY])

    def _add_ha_vips_config(self, hacluster):
        """Add a VirtualIP object for each user specified vip to self.resources

        @param hacluster instance of interface class HAClusterRequires
        """
        for vip in self.config.get(VIP_KEY, '').split():
            iface = (ch_ip.get_iface_for_address(vip) or
                     self.config.get(IFACE_KEY))
            netmask = (ch_ip.get_netmask_for_address(vip) or
                       self.config.get(CIDR_KEY))
            if iface is not None:
                hacluster.add_vip(self.name, vip, iface, netmask)

    def _add_ha_haproxy_config(self, hacluster):
        """Add a InitService object for haproxy to self.resources

        @param hacluster instance of interface class HAClusterRequires
        """
        hacluster.add_init_service(self.name, 'haproxy')

    def set_haproxy_stat_password(self):
        """Set a stats password for accessing haproxy statistics"""
        if not charms.reactive.bus.get_state('haproxy.stat.password'):
            password = ''.join([
                random.choice(string.ascii_letters + string.digits)
                for n in range(32)])
            charms.reactive.bus.set_state('haproxy.stat.password', password)

    def enable_modules(self):
        cmd = ['a2enmod', 'ssl', 'proxy', 'proxy_http']
        subprocess.check_call(cmd)

    def configure_cert(self, cert, key, cn=None):
        if not cn:
            cn = os_ip.resolve_address(endpoint_type=os_ip.INTERNAL)
        ssl_dir = os.path.join('/etc/apache2/ssl/', self.name)
        ch_host.mkdir(path=ssl_dir)
        if cn:
            cert_filename = 'cert_{}'.format(cn)
            key_filename = 'key_{}'.format(cn)
        else:
            cert_filename = 'cert'
            key_filename = 'key'

        ch_host.write_file(path=os.path.join(ssl_dir, cert_filename),
                           content=cert)
        ch_host.write_file(path=os.path.join(ssl_dir, key_filename),
                           content=key)

    def get_local_addresses(self):
        addresses = [
            os_utils.get_host_ip(hookenv.unit_get('private-address'))]
        for addr_type in os_adapters.ADDRESS_TYPES:
            cfg_opt = 'os-{}-network'.format(addr_type)
            laddr = ch_ip.get_address_in_network(self.config.get(cfg_opt))
            if laddr:
                addresses.append(laddr)
        return sorted(list(set(addresses)))

    def get_certs_and_keys(self, keystone_interface=None):
        if self.config_defined_ssl_key and self.config_defined_ssl_cert:
            return [{
                'key': self.config_defined_ssl_key.decode('utf-8'),
                'cert': self.config_defined_ssl_cert.decode('utf-8'),
                'ca': self.config_defined_ssl_ca.decode('utf-8'),
                'cn': None}]
        elif keystone_interface:
            keys_and_certs = []
            for addr in self.get_local_addresses():
                key = keystone_interface.get_remote(
                    'ssl_key_{}'.format(addr))
                cert = keystone_interface.get_remote(
                    'ssl_cert_{}'.format(addr))
                if key and cert:
                    keys_and_certs.append({
                        'key': base64.b64decode(key),
                        'cert': base64.b64decode(cert),
                        'ca': base64.b64decode(keystone_interface.ca_cert()),
                        'cn': addr})
            return keys_and_certs
        else:
            return []

    def set_config_defined_certs_and_keys(self):
        for ssl_param in ['ssl_key', 'ssl_cert', 'ssl_ca']:
            key = 'config_defined_{}'.format(ssl_param)
            if self.config.get(ssl_param):
                setattr(self, key,
                        base64.b64decode(self.config.get(ssl_param)))
            else:
                setattr(self, key, None)

    def configure_ssl(self, keystone_interface=None, cn=None):
        ssl_objects = self.get_certs_and_keys(
            keystone_interface=keystone_interface)
        if ssl_objects:
            self.enable_modules()
            for ssl in ssl_objects:
                self.configure_cert(ssl['cert'], ssl['key'], cn=ssl['cn'])
                self.configure_ca(ssl['ca'], update_certs=False)
            self.run_update_certs()
            charms.reactive.bus.set_state('ssl.enabled', True)
        else:
            charms.reactive.bus.set_state('ssl.enabled', False)

    def configure_ca(self, ca_cert, update_certs=True):
        cert_file = \
            '/usr/local/share/ca-certificates/keystone_juju_ca_cert.crt'
        if ca_cert:
            with open(cert_file, 'wb') as crt:
                crt.write(ca_cert)
            if update_certs:
                self.run_update_certs()

    def run_update_certs(self):
        subprocess.check_call(['update-ca-certificates', '--fresh'])
