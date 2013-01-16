"""Container for thrift services."""

VERSION = (0, 1, 16)

__version__ = '.'.join(map(str, VERSION[0:3]))
__author__ = 'Lipin Dmitriy'
__contact__ = 'blackwithwhite666@gmail.com'
__homepage__ = 'https://github.com/blackwithwhite666/thriftpool'
__docformat__ = 'restructuredtext'

# -eof meta-

from thriftpool.app._state import current_app as thriftpool
from thriftpool.base import BaseHandler

__all__ = ['thriftpool', 'BaseHandler']
