# -*- coding: utf-8 -*-
from zope.security.checker import undefineChecker
from plone.dexterity.fti import DexterityFTI
from plone.dexterity.fti import register
from plone.server import _
from plone.server import DEFAULT_LAYER
from plone.server import DEFAULT_PERMISSION
from plone.server import DICT_LANGUAGES
from plone.server import DICT_METHODS
from plone.server import DICT_RENDERS
from plone.server.auth.oauth import IOAuth
from plone.server.catalog.interfaces import ICatalogUtility
from plone.server.security import ViewPermissionChecker
from plone.server.utils import import_class
from plone.server.interfaces import IApplication
from plone.server.content import StaticDirectory
from zope.component.zcml import adapter
from zope.component.zcml import utility
from zope.component import getUtility
from zope.configuration import fields as configuration_fields
from zope.configuration.exceptions import ConfigurationError
from zope.configuration.fields import Path
from zope.interface import Interface
from zope.security.checker import defineChecker
from zope.security.checker import getCheckerForInstancesOf
from collections import OrderedDict

import json
import logging
import os
from pathlib import Path as osPath


logger = logging.getLogger(__name__)


class IContentTypeDirective(Interface):

    portal_type = configuration_fields.MessageID(
        title=_('Portal type'),
        description='',
        required=True
    )

    class_ = configuration_fields.GlobalObject(
        title=_('Class'),
        description='',
        required=False
    )

    schema = configuration_fields.GlobalInterface(
        title='',
        description='',
        required=True
    )

    behaviors = configuration_fields.Tokens(
        title='',
        description='',
        value_type=configuration_fields.GlobalInterface(),
        required=False
    )


def contenttypeDirective(_context,
                         portal_type,
                         class_,
                         schema,
                         behaviors=[],
                         add_permission=None):
    ''' Generate a Dexterity FTI and factory for the passed schema '''
    interface_name = schema.__identifier__
    behavior_names = [a.__identifier__ for a in behaviors]
    dotted_name = None
    if class_:
        if not hasattr(class_, 'meta_type'):
            class_.meta_type = portal_type
        dotted_name = '{0}.{1}'.format(class_.__module__, class_.__name__)
    fti_args = {'id': portal_type,
                'klass': dotted_name,
                'schema': interface_name,
                'behaviors': behavior_names}
    if add_permission is not None:
        fti_args['add_permission'] = add_permission

    fti = DexterityFTI(**fti_args)

    register(fti)


class IApi(Interface):
    '''
    '''

    file = Path(
        title='The name of a file defining the api.',
        description='Refers to a file containing a json definition.',
        required=False
    )


def register_service(
        _context,
        configuration,
        content,
        method,
        layer,
        default_permission,
        name=''):
    logger.debug(configuration)
    factory = import_class(configuration['factory'])
    if factory is None:
        raise TypeError(
            'Factory not defined {0:s} '.format(configuration['factory']))
    if getCheckerForInstancesOf(factory):
        # in case already exist remove old checker
        undefineChecker(factory)
    if 'permission' in configuration:
        permission = configuration['permission']
    else:
        permission = default_permission
    required = {}
    for n in ('__call__', 'publishTraverse'):
        required[n] = permission

    defineChecker(factory, ViewPermissionChecker(required))
    logger.debug('Defining adapter for '  # noqa
                 '{0:s} {1:s} {2:s} to {3:s} name {4:s}'.format(
        content.__identifier__,
        DICT_METHODS[method].__identifier__,
        layer.__identifier__,
        str(factory),
        name))
    adapter(
        _context,
        factory=(factory,),
        provides=DICT_METHODS[method],
        for_=(content, layer),
        name=name
    )


def apiDirective(_context, file):  # noqa 'too complex' :)

    if file:
        file = os.path.abspath(_context.path(file))
        if not os.path.isfile(file):
            raise ConfigurationError('No such file', file)

    with open(file, 'r') as f:
        json_info = json.loads(f.read(), object_pairs_hook=OrderedDict)
        f.close()

    if 'methods' in json_info:
        for method, method_interface in json_info['methods'].items():
            DICT_METHODS[method] = import_class(method_interface)

    if 'layer' in json_info:
        layer = json_info['layer']
        layer = import_class(layer)
        if len(DEFAULT_LAYER) == 0:
            DEFAULT_LAYER.append(layer)
    else:
        layer = DEFAULT_LAYER[0]

    if 'default_permission' in json_info:
        default_permission = json_info['default_permission']
        if len(DEFAULT_PERMISSION) == 0:
            DEFAULT_PERMISSION.append(default_permission)
    else:
        default_permission = DEFAULT_PERMISSION[0]

    if 'renderers' in json_info:
        for accept, renderer_interface in json_info['renderers'].items():
            # We define which Interface is for the content negotiation
            # Order is important !!
            DICT_RENDERS[accept] = import_class(renderer_interface)

    if 'languages' in json_info:
        for language, language_interface in json_info['languages'].items():
            # We define which Interface is for the languages
            logger.debug(language_interface)
            DICT_LANGUAGES[language] = import_class(language_interface)

    if 'contenttypes' in json_info:
        for contenttype, configuration in json_info['contenttypes'].items():
            content_interface = import_class(contenttype)
            for method, method_configuration in configuration.items():
                if method != 'endpoints':
                    register_service(
                        _context,
                        method_configuration,
                        content_interface,
                        method,
                        layer,
                        default_permission)

            if 'endpoints' in configuration:
                for endpoint, endpoint_configuration in configuration['endpoints'].items():  # noqa
                    for method, method_configuration in endpoint_configuration.items():  # noqa
                        register_service(
                            _context,
                            method_configuration,
                            content_interface,
                            method,
                            layer,
                            default_permission,
                            endpoint)


class IOAuthDirective(Interface):
    '''
    '''

    file = Path(
        title='The name of a file defining oauth registration information.',
        description='Refers to a file containing a json definition.',
        required=True
    )


def oauthDirective(_context, file):
    if file:
        file = os.path.abspath(_context.path(file))
        if not os.path.isfile(file):
            raise ConfigurationError('No such file', file)

    with open(file, 'r') as f:
        json_info = json.loads(f.read())
        f.close()

    OAuth = import_class(json_info['utility'])  # noqa
    settings = json_info['settings']
    oauth_utility = OAuth(settings)

    utility(_context, provides=IOAuth, component=oauth_utility)


class ICatalog(Interface):
    '''
    '''

    file = Path(
        title='The name of a file defining catalog registration information.',
        description='Refers to a file containing a json definition.',
        required=True
    )


def catalogDirective(_context, file):
    if file:
        file = os.path.abspath(_context.path(file))
        if not os.path.isfile(file):
            raise ConfigurationError('No such file', file)

    with open(file, 'r') as f:
        json_info = json.loads(f.read())
        f.close()

    CatalogUtility = import_class(json_info['utility'])
    settings = json_info['settings']
    catalog_utility = CatalogUtility(settings)

    utility(_context, provides=ICatalogUtility, component=catalog_utility)


class IResourceDirectory(Interface):
    '''
    '''

    name = configuration_fields.MessageID(
        title=_('Name where is going to be published'),
        description='',
        required=True
    )

    directory = Path(
        title='The name of the directory',
        description='Publish at /static the directory',
        required=True
    )


def resourceDirectory(_context, name, directory):
    if directory:
        directory = osPath(_context.path(directory))
        if not directory.is_dir():
            raise ConfigurationError('No such directory', directory)
    root = getUtility(IApplication, 'root')
    if name not in root:
        root[name] = StaticDirectory(directory)