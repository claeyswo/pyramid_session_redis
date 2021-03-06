# -*- coding: utf-8 -*-

import functools
import time

from pyramid.exceptions import ConfigurationError
from pyramid.session import (
    signed_deserialize,
    signed_serialize,
)

from .compat import cPickle
from .connection import get_default_connection
from .exceptions import InvalidSession, InvalidSession_NoSessionCookie
from .session import RedisSession
from .util import (
    _generate_session_id,
    _parse_settings,
    create_unique_session_id,
    warn_future,
    empty_session_payload,
    LAZYCREATE_SESSION,
)


__VERSION__ = '1.4.1-dev'


configs_dotable = ('client_callable',
                   'serialize',
                   'deserialize',
                   'id_generator',
                   'func_check_response_allow_cookies',
                   'func_invalid_logger',
                   )


configs_bool = ('cookie_on_exception',
                'cookie_secure',
                'cookie_httponly',
                'assume_redis_lru',
                'detect_changes',
                'deserialized_fails_new',
                )


def check_response_allow_cookies(response):
    """
    reference implementation for ``func_check_response_allow_cookies``
    If view has set any of these response headers, do not add a session
    cookie on this response. This way views generating cacheable content,
    like images, can signal the downstream web server that this content
    is safe. Otherwise if we set a cookie on these responses it could
    result to user session leakage.
    """
    # The view signals this is cacheable response
    # and we should not stamp a session cookie on it
    cookieless_headers = ["expires", "cache-control", ]
    for header in cookieless_headers:
        if header in response.headers:
            return False
    return True


def includeme(config):
    """
    This function is detected by Pyramid so that you can easily include
    `pyramid_session_redis` in your `main` method like so::

        config.include('pyramid_session_redis')

    Parameters:

    ``config``
    A Pyramid ``config.Configurator``
    """
    settings = config.registry.settings

    # special rule for converting dotted python paths to callables
    for option in configs_dotable:
        key = 'redis.sessions.%s' % option
        if key in settings:
            settings[key] = config.maybe_dotted(settings[key])
    session_factory = session_factory_from_settings(settings)
    config.set_session_factory(session_factory)


def session_factory_from_settings(settings):
    """
    Convenience method to construct a ``RedisSessionFactory`` from Paste config
    settings. Only settings prefixed with "redis.sessions" will be inspected
    and, if needed, coerced to their appropriate types (for example, casting
    the ``timeout`` value as an `int`).

    Parameters:

    ``settings``
    A dict of Pyramid application settings
    """
    options = _parse_settings(settings)
    return RedisSessionFactory(**options)


def RedisSessionFactory(
    secret,
    timeout=1200,
    cookie_name='session',
    cookie_max_age=None,
    cookie_path='/',
    cookie_domain=None,
    cookie_secure=False,
    cookie_httponly=True,
    cookie_on_exception=True,
    url=None,
    host='localhost',
    port=6379,
    db=0,
    password=None,
    socket_timeout=None,
    connection_pool=None,
    charset='utf-8',
    errors='strict',
    unix_socket_path=None,
    client_callable=None,
    serialize=cPickle.dumps,
    deserialize=cPickle.loads,
    id_generator=_generate_session_id,
    set_redis_ttl=True,
    set_redis_ttl_readheavy=None,
    detect_changes=True,
    deserialized_fails_new=None,
    func_check_response_allow_cookies=None,
    func_invalid_logger=None,
    timeout_trigger=None,
    python_expires=True,
):
    """
    Constructs and returns a session factory that will provide session data
    from a Redis server. The returned factory can be supplied as the
    ``session_factory`` argument of a :class:`pyramid.config.Configurator`
    constructor, or used as the ``session_factory`` argument of the
    :meth:`pyramid.config.Configurator.set_session_factory` method.

    Parameters:

    ``secret``
    A string which is used to sign the cookie.

    ``timeout``
    A number of seconds of inactivity before a session times out.
    If set to 0 or None, no timeout will occur or be managed in Redis or Python.

    ``cookie_name``
    The name of the cookie used for sessioning. Default: ``session``.

    ``cookie_max_age``
    The maximum age of the cookie used for sessioning (in seconds).
    Default: ``None`` (browser scope).

    ``cookie_path``
    The path used for the session cookie. Default: ``/``.

    ``cookie_domain``
    The domain used for the session cookie. Default: ``None`` (no domain).

    ``cookie_secure``
    The 'secure' flag of the session cookie. Default: ``False``.

    ``cookie_httponly``
    The 'httpOnly' flag of the session cookie. Default: ``True``.

    ``cookie_on_exception``
    If ``True``, set a session cookie even if an exception occurs
    while rendering a view. Default: ``True``.

    ``url``
    A connection string for a Redis server, in the format:
    redis://username:password@localhost:6379/0
    Default: ``None``.

    ``host``
    A string representing the IP of your Redis server. Default: ``localhost``.

    ``port``
    An integer representing the port of your Redis server. Default: ``6379``.

    ``db``
    An integer to select a specific database on your Redis server.
    Default: ``0``

    ``password``
    A string password to connect to your Redis server/database if
    required. Default: ``None``.

    ``client_callable``
    A python callable that accepts a Pyramid `request` and Redis config options
    and returns a Redis client such as redis-py's `StrictRedis`.
    Default: ``None``.

    ``serialize``
    A function to serialize the session dict for storage in Redis.
    Default: ``cPickle.dumps``.

    ``deserialize``
    A function to deserialize the stored session data in Redis.
    Default: ``cPickle.loads``.

    ``id_generator``
    A function to create a unique ID to be used as the session key when a
    session is first created.
    Default: private function that uses sha1 with the time and random elements
    to create a 40 character unique ID.

    ``set_redis_ttl``
    Boolean value.  Default `True`. If set to ``True``, will set a TTL. If
    ``False`` will not set a TTL and assumes that Redis is configured as a
    least-recently-used cache [http://redis.io/topics/lru-cache] and will NOT
    send EXPIRY data of sessions to Redis (the value of `timeout` will be
    ignored if set). This does not require or imply that no ``timeout`` data is
    handled within the Python payload, it just determines if Redis will be
    involved with timeout logic.
    Default: ``False``

    ``set_redis_ttl_readheavy``
     If ``True``, sets TTL data in Redis within a PIPELINE via GET+EXPIRE and
     supresses automatic TTL refresh during the deferred cleanup phase. If not 
     ``True``, an EXPIRE is sent as a separate action during the deferred
     cleanup phase.  The optimized behavior improves performance on read-heavy
     operations, but may degrade performance on write-heavy operations.  This
     requires a ``timeout`` and ``set_redis_ttl`` to be True; it is not
     compatible with ``timeout_trigger`` or ``python_expires``.
     Default: ``None``

    ``detect_changes``
    Boolean value. If set to ``True``, will calculate nested changes after
    serialization to ensure persistence of nested data.
    Default: ``True``

    ``deserialized_fails_new``
    If ``True`` will handle deserializtion errors by creating a new session.

    ``func_check_response_allow_cookies``
    A callable function that accepts a response, returning ``True`` if the
    cookie can be sent and ``False`` if it should not.  This defaults to
    ``None``.  An example callable is available in
    ``check_response_allow_cookies``, which checks for `expires` and
    `cache-control` cookies.

    ``python_expires``
    Int, default ``True``.  If ``True``, allows for timeout logic to be tracked
    in Python

    ``func_invalid_logger``
    A callable function that expects a single argument of a raised
    `InvalidSession` exception. If not ``None``, this will be called so your
    application can monitor.

    ``timeout_trigger``
    Int, default  ``None``.  If unset or ``0``, timeouts will be updated on
    every access by setting an EXPIRY in Redis and/or updating the ``expires``
    value in the  session payload.  If set to an INT, the updates will only be
    set once the threshold is crossed.

    Given this example:

        timeout = 1200
        timeout_trigger = 600

    The following timeline would occur

        time | action | timeout | next threshold
        -----+--------+---------+--------------
           0 | CREATE | 1200    | 600
         599 |        | 1200    | 600
         600 | UPDATE | 1800    | 1200
         599 |        | 1800    | 1200
        1200 | UPDATE | 2400    | 1800

    The feature has the ability to significantly lower the amount of processing
    on Redis, however it means a session timeout expires after the last trigger
    and not the last usage.

    For example, with a timeout trigger of 10 minutes on a 1 hour session, if a
    user leaves the site at 49 minutes and returns at 61 minutes, the trigger
    will not have been made and the session will have expired.

    The following arguments are also passed straight to the ``StrictRedis``
    constructor and allow you to further configure the Redis client::

      socket_timeout
      connection_pool
      charset
      errors
      unix_socket_path
    """
    if timeout == 0:
        timeout = None

    if timeout_trigger and not python_expires:  # fix this
        python_expires = True

    # optimize a `TTL refresh` under certain conditions
    if set_redis_ttl_readheavy:
        if (not timeout) or (not set_redis_ttl):
            raise ValueError("`set_redis_ttl_readheavy` requires a `timeout` and `set_redis_ttl`")
        if timeout_trigger or python_expires:
            raise ValueError("`set_redis_ttl_readheavy` is not compatible with `timeout_trigger` and `python_expires`")
    optimize_redis_ttl = False
    
    _set_redis_ttl_onexit = False
    if (timeout and set_redis_ttl) and (not timeout_trigger and not python_expires and not set_redis_ttl_readheavy):
        _set_redis_ttl_onexit = True

    # good for all factory() requests
    redis_options = dict(
        host=host,
        port=port,
        db=db,
        password=password,
        socket_timeout=socket_timeout,
        connection_pool=connection_pool,
        charset=charset,
        errors=errors,
        unix_socket_path=unix_socket_path,
    )

    # good for all factory() requests
    new_payload_func = functools.partial(
        empty_session_payload,
        timeout=timeout,
        python_expires=python_expires,
    )

    # good for all factory() requests
    delete_cookie_func = functools.partial(
        _delete_cookie,
        cookie_name=cookie_name,
        cookie_path=cookie_path,
        cookie_domain=cookie_domain,
    )

    def factory(request, new_session_id_func=create_unique_session_id):

        # an explicit client callable gets priority over the default
        redis_conn = client_callable(request, **redis_options) \
            if client_callable is not None \
            else get_default_connection(request, url=url, **redis_options)

        new_session_func = functools.partial(
            new_session_id_func,
            redis=redis_conn,
            timeout=timeout,
            serialize=serialize,
            generator=id_generator,
            set_redis_ttl=set_redis_ttl,
            # set_redis_ttl_readheavy=set_redis_ttl_readheavy,  # not needed on NEW
            # _set_redis_ttl_onexit=_set_redis_ttl_onexit,  # not needed on NEW
            new_payload_func=new_payload_func,
            python_expires=python_expires,
        )

        try:
            # attempt to retrieve a session_id from the cookie
            session_id = _get_session_id_from_cookie(
                request=request,
                cookie_name=cookie_name,
                secret=secret,
            )
            if not session_id:
                raise InvalidSession_NoSessionCookie('No `session_id` in cookie.')
            session_cookie_was_valid = True
            session = RedisSession(
                redis=redis_conn,
                session_id=session_id,
                new=False,
                new_session=new_session_func,
                new_payload_func=new_payload_func,
                serialize=serialize,
                deserialize=deserialize,
                set_redis_ttl=set_redis_ttl,
                set_redis_ttl_readheavy=set_redis_ttl_readheavy,
                _set_redis_ttl_onexit=_set_redis_ttl_onexit,
                detect_changes=detect_changes,
                deserialized_fails_new=deserialized_fails_new,
                timeout_trigger=timeout_trigger,
                timeout=timeout,
                python_expires=python_expires,
            )
        except InvalidSession as e:
            if func_invalid_logger is not None:
                # send the instance for logging
                func_invalid_logger(request, e)
            session_id = LAZYCREATE_SESSION
            session_cookie_was_valid = False
            session = RedisSession(
                redis=redis_conn,
                session_id=session_id,
                new=True,
                new_session=new_session_func,
                new_payload_func=new_payload_func,
                serialize=serialize,
                deserialize=deserialize,
                set_redis_ttl=set_redis_ttl,
                # set_redis_ttl_readheavy=set_redis_ttl_readheavy,  # not needed on NEW
                # _set_redis_ttl_onexit=_set_redis_ttl_onexit,  # not needed on NEW
                detect_changes=detect_changes,
                timeout_trigger=timeout_trigger,
                timeout=timeout,
                python_expires=python_expires,
            )

        set_cookie_func = functools.partial(
            _set_cookie,
            session,
            cookie_name=cookie_name,
            cookie_max_age=cookie_max_age,
            cookie_path=cookie_path,
            cookie_domain=cookie_domain,
            cookie_secure=cookie_secure,
            cookie_httponly=cookie_httponly,
            secret=secret,
        )
        cookie_callback = functools.partial(
            _cookie_callback,
            session,
            session_cookie_was_valid=session_cookie_was_valid,
            cookie_on_exception=cookie_on_exception,
            set_cookie_func=set_cookie_func,
            delete_cookie_func=delete_cookie_func,
            func_check_response_allow_cookies=func_check_response_allow_cookies,
        )
        request.add_response_callback(cookie_callback)

        request.add_finished_callback(session._deferred_callback)

        return session

    return factory


def _get_session_id_from_cookie(request, cookie_name, secret):
    """
    Attempts to retrieve and return a session ID from a session cookie in the
    current request. Returns None if the cookie isn't found or the value cannot
    be deserialized for any reason.
    """
    cookieval = request.cookies.get(cookie_name)

    if cookieval is not None:
        try:
            session_id = signed_deserialize(cookieval, secret)
            return session_id
        except ValueError:
            pass

    return None


def _set_cookie(
    session,
    request,
    response,
    cookie_name,
    cookie_max_age,
    cookie_path,
    cookie_domain,
    cookie_secure,
    cookie_httponly,
    secret,
):
    """
    `session` is via functools.partial
    `request` and `response` are appended by add_response_callback
    """
    cookieval = signed_serialize(session.session_id, secret)
    response.set_cookie(
        cookie_name,
        cookieval,
        max_age=cookie_max_age,
        path=cookie_path,
        domain=cookie_domain,
        secure=cookie_secure,
        httponly=cookie_httponly,
    )


def _delete_cookie(response, cookie_name, cookie_path, cookie_domain):
    response.delete_cookie(cookie_name, path=cookie_path, domain=cookie_domain)


def _cookie_callback(
    session,
    request,
    response,
    session_cookie_was_valid,
    cookie_on_exception,
    set_cookie_func,
    delete_cookie_func,
    func_check_response_allow_cookies,
):
    """
    Response callback to set the appropriate Set-Cookie header.
    `session` is via functools.partial
    `request` and `response` are appended by add_response_callback
    """
    if func_check_response_allow_cookies is not None:
        if not func_check_response_allow_cookies(response):
            # if we don't want to send cookies on this response,
            # we might not want to persist or refresh
            # session._session_state.dont_persist = True
            # session._session_state.dont_refresh = True
            return
    if session._invalidated:
        if session_cookie_was_valid:
            delete_cookie_func(response=response)
        return
    if session.new:
        if not session.session_id_safecheck:
            return
        if cookie_on_exception is True or request.exception is None:
            set_cookie_func(request=request, response=response)

            # If we set a cookie we need to make sure that downstream
            # web servicess do not serve this response from a cache
            # for requests coming in with a different session cookie.
            # Otherwise we might leak sessions between users.
            varies = ("Cookie", )
            vary = set(response.vary if response.vary is not None else [])
            vary |= set(varies)
            response.vary = vary

        elif session_cookie_was_valid:
            # We don't set a cookie for the new session here (as
            # cookie_on_exception is False and an exception was raised), but we
            # still need to delete the existing cookie for the session that the
            # request started with (as the session has now been invalidated).
            delete_cookie_func(response=response)
