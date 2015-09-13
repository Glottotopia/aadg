"""passlib.bcrypt -- implementation of OpenBSD's BCrypt algorithm.

TODO:

* support 2x and altered-2a hashes?
  http://www.openwall.com/lists/oss-security/2011/06/27/9

* deal with lack of PY3-compatibile c-ext implementation
"""
#=============================================================================
# imports
#=============================================================================
from __future__ import with_statement, absolute_import
# core
import os
import re
import logging; log = logging.getLogger(__name__)
from warnings import warn
# site
try:
    from bcrypt import hashpw as pybcrypt_hashpw
except ImportError: # pragma: no cover
    pybcrypt_hashpw = None
try:
    from bcryptor.engine import Engine as bcryptor_engine
except ImportError: # pragma: no cover
    bcryptor_engine = None
# pkg
from passlib.exc import PasslibHashWarning
from passlib.utils import bcrypt64, safe_crypt, repeat_string, \
                          classproperty, rng, getrandstr, test_crypt
from passlib.utils.compat import bytes, b, u, uascii_to_str, unicode, str_to_uascii
import passlib.utils.handlers as uh

# local
__all__ = [
    "bcrypt",
]

#=============================================================================
# support funcs & constants
#=============================================================================
_builtin_bcrypt = None

def _load_builtin():
    global _builtin_bcrypt
    if _builtin_bcrypt is None:
        from passlib.utils._blowfish import raw_bcrypt as _builtin_bcrypt

IDENT_2 = u("$2$")
IDENT_2A = u("$2a$")
IDENT_2X = u("$2x$")
IDENT_2Y = u("$2y$")
_BNULL = b('\x00')

#=============================================================================
# handler
#=============================================================================
class bcrypt(uh.HasManyIdents, uh.HasRounds, uh.HasSalt, uh.HasManyBackends, uh.GenericHandler):
    """This class implements the BCrypt password hash, and follows the :ref:`password-hash-api`.

    It supports a fixed-length salt, and a variable number of rounds.

    The :meth:`~passlib.ifc.PasswordHash.encrypt` and :meth:`~passlib.ifc.PasswordHash.genconfig` methods accept the following optional keywords:

    :type salt: str
    :param salt:
        Optional salt string.
        If not specified, one will be autogenerated (this is recommended).
        If specified, it must be 22 characters, drawn from the regexp range ``[./0-9A-Za-z]``.

    :type rounds: int
    :param rounds:
        Optional number of rounds to use.
        Defaults to 12, must be between 4 and 31, inclusive.
        This value is logarithmic, the actual number of iterations used will be :samp:`2**{rounds}`
        -- increasing the rounds by +1 will double the amount of time taken.

    :type ident: str
    :param ident:
        Specifies which version of the BCrypt algorithm will be used when creating a new hash.
        Typically this option is not needed, as the default (``"2a"``) is usually the correct choice.
        If specified, it must be one of the following:

        * ``"2"`` - the first revision of BCrypt, which suffers from a minor security flaw and is generally not used anymore.
        * ``"2a"`` - latest revision of the official BCrypt algorithm, and the current default.
        * ``"2y"`` - format specific to the *crypt_blowfish* BCrypt implementation,
          identical to ``"2a"`` in all but name.

    :type relaxed: bool
    :param relaxed:
        By default, providing an invalid value for one of the other
        keywords will result in a :exc:`ValueError`. If ``relaxed=True``,
        and the error can be corrected, a :exc:`~passlib.exc.PasslibHashWarning`
        will be issued instead. Correctable errors include ``rounds``
        that are too small or too large, and ``salt`` strings that are too long.

        .. versionadded:: 1.6

    .. versionchanged:: 1.6
        This class now supports ``"2y"`` hashes, and recognizes
        (but does not support) the broken ``"2x"`` hashes.
        (see the :ref:`crypt_blowfish bug <crypt-blowfish-bug>`
        for details).

    .. versionchanged:: 1.6
        Added a pure-python backend.
    """

    #===================================================================
    # class attrs
    #===================================================================
    #--GenericHandler--
    name = "bcrypt"
    setting_kwds = ("salt", "rounds", "ident")
    checksum_size = 31
    checksum_chars = bcrypt64.charmap

    #--HasManyIdents--
    default_ident = u("$2a$")
    ident_values = (u("$2$"), IDENT_2A, IDENT_2X, IDENT_2Y)
    ident_aliases = {u("2"): u("$2$"), u("2a"): IDENT_2A,  u("2y"): IDENT_2Y}

    #--HasSalt--
    min_salt_size = max_salt_size = 22
    salt_chars = bcrypt64.charmap
        # NOTE: 22nd salt char must be in bcrypt64._padinfo2[1], not full charmap

    #--HasRounds--
    default_rounds = 12 # current passlib default
    min_rounds = 4 # bcrypt spec specified minimum
    max_rounds = 31 # 32-bit integer limit (since real_rounds=1<<rounds)
    rounds_cost = "log2"

    #===================================================================
    # formatting
    #===================================================================

    @classmethod
    def from_string(cls, hash):
        ident, tail = cls._parse_ident(hash)
        if ident == IDENT_2X:
            raise ValueError("crypt_blowfish's buggy '2x' hashes are not "
                             "currently supported")
        rounds_str, data = tail.split(u("$"))
        rounds = int(rounds_str)
        if rounds_str != u('%02d') % (rounds,):
            raise uh.exc.MalformedHashError(cls, "malformed cost field")
        salt, chk = data[:22], data[22:]
        return cls(
            rounds=rounds,
            salt=salt,
            checksum=chk or None,
            ident=ident,
        )

    def to_string(self):
        hash = u("%s%02d$%s%s") % (self.ident, self.rounds, self.salt,
                                   self.checksum or u(''))
        return uascii_to_str(hash)

    def _get_config(self, ident=None):
        "internal helper to prepare config string for backends"
        if ident is None:
            ident = self.ident
        if ident == IDENT_2Y:
            ident = IDENT_2A
        else:
            assert ident != IDENT_2X
        config = u("%s%02d$%s") % (ident, self.rounds, self.salt)
        return uascii_to_str(config)

    #===================================================================
    # specialized salt generation - fixes passlib issue 25
    #===================================================================

    @classmethod
    def _bind_needs_update(cls, **settings):
        return cls._needs_update

    @classmethod
    def _needs_update(cls, hash, secret):
        if isinstance(hash, bytes):
            hash = hash.decode("ascii")
        # check for incorrect padding bits (passlib issue 25)
        if hash.startswith(IDENT_2A) and hash[28] not in bcrypt64._padinfo2[1]:
            return True
        # TODO: try to detect incorrect $2x$ hashes using *secret*
        return False

    @classmethod
    def normhash(cls, hash):
        "helper to normalize hash, correcting any bcrypt padding bits"
        if cls.identify(hash):
            return cls.from_string(hash).to_string()
        else:
            return hash

    def _generate_salt(self, salt_size):
        # override to correct generate salt bits
        salt = super(bcrypt, self)._generate_salt(salt_size)
        return bcrypt64.repair_unused(salt)

    def _norm_salt(self, salt, **kwds):
        salt = super(bcrypt, self)._norm_salt(salt, **kwds)
        assert salt is not None, "HasSalt didn't generate new salt!"
        changed, salt = bcrypt64.check_repair_unused(salt)
        if changed:
            # FIXME: if salt was provided by user, this message won't be
            # correct. not sure if we want to throw error, or use different warning.
            warn(
                "encountered a bcrypt salt with incorrectly set padding bits; "
                "you may want to use bcrypt.normhash() "
                "to fix this; see Passlib 1.5.3 changelog.",
                PasslibHashWarning)
        return salt

    def _norm_checksum(self, checksum):
        checksum = super(bcrypt, self)._norm_checksum(checksum)
        if not checksum:
            return None
        changed, checksum = bcrypt64.check_repair_unused(checksum)
        if changed:
            warn(
                "encountered a bcrypt hash with incorrectly set padding bits; "
                "you may want to use bcrypt.normhash() "
                "to fix this; see Passlib 1.5.3 changelog.",
                PasslibHashWarning)
        return checksum

    #===================================================================
    # primary interface
    #===================================================================
    backends = ("pybcrypt", "bcryptor", "os_crypt", "builtin")

    @classproperty
    def _has_backend_pybcrypt(cls):
        return pybcrypt_hashpw is not None

    @classproperty
    def _has_backend_bcryptor(cls):
        return bcryptor_engine is not None

    @classproperty
    def _has_backend_builtin(cls):
        if os.environ.get("PASSLIB_BUILTIN_BCRYPT") not in ["enable","enabled"]:
            return False
        # look at it cross-eyed, and it loads itself
        _load_builtin()
        return True

    @classproperty
    def _has_backend_os_crypt(cls):
        # XXX: what to do if only h2 is supported? h1 is *very* rare.
        h1 = '$2$04$......................1O4gOrCYaqBG3o/4LnT2ykQUt1wbyju'
        h2 = '$2a$04$......................qiOQjkB8hxU8OzRhS.GhRMa4VUnkPty'
        return test_crypt("test",h1) and test_crypt("test", h2)

    @classmethod
    def _no_backends_msg(cls):
        return "no bcrypt backends available - please install py-bcrypt"

    def _calc_checksum_os_crypt(self, secret):
        config = self._get_config()
        hash = safe_crypt(secret, config)
        if hash:
            assert hash.startswith(config) and len(hash) == len(config)+31
            return hash[-31:]
        else:
            # NOTE: it's unlikely any other backend will be available,
            # but checking before we bail, just in case.
            for name in self.backends:
                if name != "os_crypt" and self.has_backend(name):
                    func = getattr(self, "_calc_checksum_" + name)
                    return func(secret)
            raise uh.exc.MissingBackendError(
                "password can't be handled by os_crypt, "
                "recommend installing py-bcrypt.",
                )

    def _calc_checksum_pybcrypt(self, secret):
        # py-bcrypt behavior:
        #   py2: unicode secret/hash encoded as ascii bytes before use,
        #        bytes taken as-is; returns ascii bytes.
        #   py3: not supported (patch submitted)
        if isinstance(secret, unicode):
            secret = secret.encode("utf-8")
        if _BNULL in secret:
            raise uh.exc.NullPasswordError(self)
        config = self._get_config()
        hash = pybcrypt_hashpw(secret, config)
        assert hash.startswith(config) and len(hash) == len(config)+31
        return str_to_uascii(hash[-31:])

    def _calc_checksum_bcryptor(self, secret):
        # bcryptor behavior:
        #   py2: unicode secret/hash encoded as ascii bytes before use,
        #        bytes taken as-is; returns ascii bytes.
        #   py3: not supported
        if isinstance(secret, unicode):
            secret = secret.encode("utf-8")
        if _BNULL in secret:
            # NOTE: especially important to forbid NULLs for bcryptor,
            # since it happily accepts them, and then silently truncates
            # the password at first one it encounters :(
            raise uh.exc.NullPasswordError(self)
        if self.ident == IDENT_2:
            # bcryptor doesn't support $2$ hashes; but we can fake $2$ behavior
            # using the $2a$ algorithm, by repeating the password until
            # it's at least 72 chars in length.
            if secret:
                secret = repeat_string(secret, 72)
            config = self._get_config(IDENT_2A)
        else:
            config = self._get_config()
        hash = bcryptor_engine(False).hash_key(secret, config)
        assert hash.startswith(config) and len(hash) == len(config)+31
        return str_to_uascii(hash[-31:])

    def _calc_checksum_builtin(self, secret):
        if isinstance(secret, unicode):
            secret = secret.encode("utf-8")
        if _BNULL in secret:
            raise uh.exc.NullPasswordError(self)
        chk = _builtin_bcrypt(secret, self.ident.strip("$"),
                              self.salt.encode("ascii"), self.rounds)
        return chk.decode("ascii")

    #===================================================================
    # eoc
    #===================================================================

#=============================================================================
# eof
#=============================================================================
