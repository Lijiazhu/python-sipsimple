#!/usr/bin/env python
# Copyright (C) 2008-2009 AG Projects. See LICENSE for details.
#

import datetime
import os
import random
import re
import select
import subprocess
import sys
import termios
import traceback

from application import log
from application.notification import IObserver, NotificationCenter, NotificationData
from application.python.queue import EventQueue
from collections import deque
from optparse import OptionParser
from threading import Thread
from time import time
from twisted.internet.error import ReactorNotRunning
from twisted.python import threadable
from zope.interface import implements

from twisted.internet import reactor
from eventlet.twistedutil import join_reactor

from sipsimple import Engine, Publication, SIPCoreError, SIPURI
from sipsimple.account import AccountManager
from sipsimple.clients.log import Logger
from sipsimple.lookup import DNSLookup
from sipsimple.configuration import ConfigurationManager
from sipsimple.configuration.settings import SIPSimpleSettings

from sipsimple.applications import BuilderError
from sipsimple.applications.presdm import Contact, Device, DeviceNote, DeviceTimestamp, Person, PersonNote, PersonTimestamp, PIDF, Service, ServiceTimestamp, Status
from sipsimple.applications.rpid import Activities, Mood, PlaceIs, Privacy, StatusIcon, TimeOffset, UserInput


class InputThread(Thread):
    def __init__(self, application):
        Thread.__init__(self)
        self.application = application
        self.daemon = True
        self._old_terminal_settings = None

    def run(self):
        notification_center = NotificationCenter()
        while True:
            for char in self._getchars():
                if char == "\x04":
                    self.application.stop()
                    return
                else:
                    notification_center.post_notification('SAInputWasReceived', sender=self, data=NotificationData(input=char))

    def stop(self):
        self._termios_restore()

    def _termios_restore(self):
        if self._old_terminal_settings is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_terminal_settings)

    def _getchars(self):
        fd = sys.stdin.fileno()
        if os.isatty(fd):
            self._old_terminal_settings = termios.tcgetattr(fd)
            new = termios.tcgetattr(fd)
            new[3] = new[3] & ~termios.ICANON & ~termios.ECHO
            new[6][termios.VMIN] = '\000'
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, new)
                if select.select([fd], [], [], None)[0]:
                    return sys.stdin.read(4192)
            finally:
                self._termios_restore()
        else:
            return os.read(fd, 4192)


class AutoPublicationApplication(object):
    implements(IObserver)

    def __init__(self, account_name, interval, trace_sip, trace_pjsip, trace_notifications):
        self.account_name = account_name
        self.interval = interval
        self.input = InputThread(self)
        self.output = EventQueue(lambda event: sys.stdout.write(event+'\n'))
        self.logger = Logger(trace_sip, trace_pjsip, trace_notifications)
        self.success = False
        self.account = None
        self.publication = None
        self.pidf = None
        self.main_service = None
        self.email_service = None
        self.person = None
        self.device = None
        self.stopping = False

        self._publication_routes = None
        self._publication_timeout = 0.0
        self._publication_wait = 0.5
        self._republish = False

        account_manager = AccountManager()
        engine = Engine()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=account_manager)
        notification_center.add_observer(self, sender=engine)
        notification_center.add_observer(self, sender=self.input)

        log.level.current = log.level.WARNING

    def run(self):
        account_manager = AccountManager()
        configuration = ConfigurationManager()
        engine = Engine()
        notification_center = NotificationCenter()
        
        # start output thread
        self.output.start()
    
        # startup configuration
        configuration.start()
        account_manager.start()
        if self.account is None:
            raise RuntimeError("unknown account %s. Available accounts: %s" % (self.account_name, ', '.join(account.id for account in account_manager.iter_accounts())))
        elif not self.account.enabled:
            raise RuntimeError("account %s is not enabled" % self.account.id)
        self.output.put('Using account %s' % self.account.id)
        settings = SIPSimpleSettings()

        # start logging
        self.logger.start()

        # start the engine
        engine.start(
            auto_sound=False,
            local_ip=settings.local_ip.normalized,
            local_udp_port=settings.sip.local_udp_port if "udp" in settings.sip.transports else None,
            local_tcp_port=settings.sip.local_tcp_port if "tcp" in settings.sip.transports else None,
            local_tls_port=settings.sip.local_tls_port if "tls" in settings.sip.transports else None,
            tls_protocol=settings.tls.protocol,
            tls_verify_server=settings.tls.verify_server,
            tls_ca_file=settings.tls.ca_list_file.normalized if settings.tls.ca_list_file is not None else None,
            tls_cert_file=settings.tls.certificate_file.normalized if settings.tls.certificate_file is not None else None,
            tls_privkey_file=settings.tls.private_key_file.normalized if settings.tls.private_key_file is not None else None,
            tls_timeout=settings.tls.timeout,
            ec_tail_length=settings.audio.echo_delay,
            user_agent=settings.user_agent,
            sample_rate=settings.audio.sample_rate,
            playback_dtmf=settings.audio.playback_dtmf,
            rtp_port_range=(settings.rtp.port_range.start, settings.rtp.port_range.end),
            trace_sip=settings.logging.trace_sip or self.logger.sip_to_stdout,
            log_level=settings.logging.pjsip_level if (settings.logging.trace_pjsip or self.logger.pjsip_to_stdout) else 0
        )
        
        # initialize pidf
        self.pidf = PIDF(entity=self.account.id) # entity will be determined when account is selected

        # initialize top level elements
        self.main_service = Service(''.join(chr(random.randint(97, 122)) for i in xrange(8)), status=Status(basic='open'))
        self.main_service.contact = Contact("sip:%s" % self.account.id)
        self.main_service.contact.priority = 0
        self.main_service.relationship = 'self'
        self.main_service.timestamp = ServiceTimestamp()
        self.pidf.append(self.main_service)

        # add email service
        self.email_service = Service(''.join(chr(random.randint(97, 122)) for i in xrange(8)), status=Status(basic='open'))
        self.email_service.contact = Contact("mailto:%s" % self.account.id)
        self.email_service.contact.priority = 0.5
        self.email_service.relationship = 'self'
        self.email_service.timestamp = ServiceTimestamp()
        self.pidf.append(self.email_service)

        self.person = Person(''.join(chr(random.randint(97, 122)) for i in xrange(8)))
        self.person.privacy = Privacy()
        self.person.time_offset = TimeOffset()
        self.person.timestamp = PersonTimestamp()
        self.pidf.append(self.person)

        self.device = Device(''.join(chr(random.randint(97, 122)) for i in xrange(8)))
        self.device.notes.add(DeviceNote('Powered by %s' % engine.user_agent, lang='en'))
        self.device.timestamp = DeviceTimestamp()
        self.device.user_input = UserInput()
        self.pidf.append(self.device)

        # start getting input
        self.input.start()

        reactor.callLater(0, self._publish)

        # start twisted
        try:
            reactor.run()
        finally:
            self.input.stop()
        
        # stop the output
        self.output.stop()
        self.output.join()
        
        self.logger.stop()

        return 0 if self.success else 1

    def stop(self):
        self.stopping = True
        account_manager = AccountManager()
        account_manager.stop()
        if self.publication is not None and self.publication.state in ('published', 'publishing'):
            self.publication.unpublish()
        else:
            engine = Engine()
            engine.stop()

    def print_help(self):
        message  = 'Available control keys:\n'
        message += '  t: toggle SIP trace on the console\n'
        message += '  j: toggle PJSIP trace on the console\n'
        message += '  n: toggle notifications trace on the console\n'
        message += '  Ctrl-d: quit the program\n'
        message += '  ?: display this help message\n'
        self.output.put('\n'+message)
        
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, None)
        if handler is not None:
            handler(notification)

    def _NH_SIPAccountManagerDidAddAccount(self, notification):
        account = notification.data.account
        account_manager = AccountManager()
        if account.id == self.account_name or (self.account_name is None and account is account_manager.default_account):
            self.account = account
            account.registration.enabled = False
        else:
            account.enabled = False

    def _NH_SIPPublicationChangedState(self, notification):
        route = notification.sender.route
        if notification.data.state == 'published':
            self._publication_routes = None
            self._publication_wait = 0.5
            self.success = True
            self.output.put('PUBLISH was successful at %s:%d;transport=%s' % (route.address, route.port, route.transport))
            if self._republish:
                self._republish = False
                reactor.callFromThread(reactor.callLater, self.interval, reactor.callInThread, self._auto_publish)
        elif notification.data.state == 'unpublished':
            self.publication = None
            self.success = False
            self.output.put('Unpublished from %s:%d;transport=%s: %d %s' % (route.address, route.port, route.transport, notification.data.code, notification.data.reason))
            if self.stopping or notification.data.code in (401, 403, 407):
                if notification.data.code / 100 == 2:
                    self.success = True
                self.stop()
            else:
                if not self._publication_routes or time() > self._publication_timeout:
                    self._publication_wait = min(self._publication_wait*2, 30)
                    timeout = random.uniform(self._publication_wait, 2*self._publication_wait)
                    reactor.callFromThread(reactor.callLater, timeout, self._publish)
                else:
                    self.publication = Publication(self.account.credentials, "presence", route=self._publication_routes.popleft(), expires=self.account.presence.publish_interval)
                    notification_center = NotificationCenter()
                    notification_center.add_observer(self, sender=self.publication)
                    reactor.callInThread(self._auto_publish)

    def _NH_DNSLookupDidSucceed(self, notification):
        # create publication and register to get notifications from it
        self._publication_routes = deque(notification.data.result)
        self.publication = Publication(self.account.credentials, "presence", route=self._publication_routes.popleft(), expires=self.account.presence.publish_interval)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.publication)
        reactor.callInThread(self._auto_publish)

    def _NH_DNSLookupDidFail(self, notification):
        self.output.put('DNS lookup failed: %s' % notification.data.error)
        timeout = random.uniform(1.0, 2.0)
        reactor.callLater(timeout, self._publish)

    def _NH_SAInputWasReceived(self, notification):
        engine = Engine()
        settings = SIPSimpleSettings()
        key = notification.data.input
        if key == 't':
            self.logger.sip_to_stdout = not self.logger.sip_to_stdout
            engine.trace_sip = self.logger.sip_to_stdout or settings.logging.trace_sip
            self.output.put('SIP tracing to console is now %s.' % ('activated' if self.logger.sip_to_stdout else 'deactivated'))
        elif key == 'j':
            self.logger.pjsip_to_stdout = not self.logger.pjsip_to_stdout
            engine.log_level = settings.logging.pjsip_level if (self.logger.pjsip_to_stdout or settings.logging.trace_pjsip) else 0
            self.output.put('PJSIP tracing to console is now %s.' % ('activated' if self.logger.pjsip_to_stdout else 'deactivated'))
        elif key == 'n':
            self.logger.notifications_to_stdout = not self.logger.notifications_to_stdout
            self.output.put('Notification tracing to console is now %s.' % ('activated' if self.logger.notifications_to_stdout else 'deactivated'))
        elif key == '?':
            self.print_help()

    def _NH_SIPEngineDidEnd(self, notification):
        if threadable.isInIOThread():
            self._stop_reactor()
        else:
            reactor.callFromThread(self._stop_reactor)

    def _NH_SIPEngineDidFail(self, notification):
        self.output.put('Engine failed.')
        if threadable.isInIOThread():
            self._stop_reactor()
        else:
            reactor.callFromThread(self._stop_reactor)

    def _NH_SIPEngineGotException(self, notification):
        self.output.put('An exception occured within the SIP core:\n'+notification.data.traceback)

    def _stop_reactor(self):
        try:
            reactor.stop()
        except ReactorNotRunning:
            pass
    
    def _publish(self):
        settings = SIPSimpleSettings()
        
        self._publication_timeout = time()+30

        lookup = DNSLookup()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=lookup)
        if self.account.outbound_proxy is not None:
            uri = SIPURI(host=self.account.outbound_proxy.host, port=self.account.outbound_proxy.port, parameters={'transport': self.account.outbound_proxy.transport})
        else:
            uri = SIPURI(host=self.account.id.domain)
        lookup.lookup_sip_proxy(uri, settings.sip.transports)

    def _random_note(self):
        try:
            fortune = subprocess.Popen('fortune', stdout=subprocess.PIPE)
            fortune.wait()
            return ' '.join(s for s in re.split(r'\n|\t', fortune.stdout.read()) if s != '')
        except:
            return 'Fortune is not installed'

    def _auto_publish(self):
        # 50% chance that basic status will change
        if random.randint(0, 1) == 1:
            if self.main_service.status.basic == 'open':
                self.main_service.status.basic = 'closed'
            else:
                self.main_service.status.basic = 'open'
            self.main_service.timestamp = ServiceTimestamp()
        
        # set sphere (9-18 at work (except on weekends), else at home)
        now = datetime.datetime.now()
        if (now.hour >= 9 and now.hour < 18) and now.isoweekday() not in (6, 7):
            self.person.sphere = 'work'
            self.person.sphere.since = datetime.datetime(now.year, now.month, now.day, 9, 0)
            self.person.sphere.until = datetime.datetime(now.year, now.month, now.day, 18, 0)
        else:
            self.person.sphere = 'home'

        # set privacy
        self.person.privacy.audio = random.choice((True, False))
        self.person.privacy.text = random.choice((True, False))
        self.person.privacy.video = random.choice((True, False))

        # set status icon
        self.person.status_icon = StatusIcon("http://sipsimpleclient.com/chrome/site/StatusIcons/%s.png" % random.choice(('available', 'busy')))

        # change person note
        if len(self.person.notes) > 0:
            del self.person.notes['en']
        self.person.notes.add(PersonNote(self._random_note(), lang='en'))
        
        # change person activity
        if self.person.activities is None:
            self.person.activities = Activities()
        else:
            self.person.activities.clear()
        values = list(value for value in Activities.values if value != 'unknown')
        for i in xrange(random.randrange(1, 3)):
            value = random.choice(values)
            values.remove(value)
            self.person.activities.append(value)

        # change person mood
        if self.person.mood is None:
            self.person.mood = Mood()
        else:
            self.person.mood.clear()
        values = list(value for value in Mood.values if value != 'unknown')
        for i in xrange(random.randrange(1, 3)):
            value = random.choice(values)
            values.remove(value)
            self.person.mood.append(value)

        # change place is
        if self.person.place_is is None:
            self.person.place_is = PlaceIs()
        # 50% chance that place is will change
        if random.randint(0, 1) == 1:
            self.person.place_is.audio = random.choice(('noisy', 'ok', 'quiet', 'unknown'))
        if random.randint(0, 1) == 1:
            self.person.place_is.video = random.choice(('toobright', 'ok', 'dark', 'unknown'))
        if random.randint(0, 1) == 1:
            self.person.place_is.text = random.choice(('uncomfortable', 'inappropriate', 'ok', 'unknown'))

        self.person.timestamp = PersonTimestamp()
        
        # set user-input
        if self.device.user_input.value == 'idle':
            # 50 % chance to change to active:
            if random.randint(0, 1) == 1:
                self.device.user_input.value = 'active'
                self.device.user_input.last_input = None
        else:
            # 50 % chance to change to idle:
            if random.randint(0, 1) == 1:
                self.device.user_input.value = 'idle'
                self.device.user_input.last_input = now - datetime.timedelta(seconds=30)
        
        # publish new pidf
        self._republish = True
        try:
            self.publication.publish("application", "pidf+xml", self.pidf.toxml())
        except BuilderError, e:
            print "PIDF as currently defined is invalid: %s" % str(e)
        except:
            traceback.print_exc()


if __name__ == "__main__":
    description = "This script will publish randomly generated rich presence state for the specified SIP account to a SIP Presence Agent."
    usage = "%prog [options]"
    parser = OptionParser(usage=usage, description=description)
    parser.print_usage = parser.print_help
    parser.add_option("-a", "--account-name", type="string", dest="account_name", help="The name of the account to use.")
    parser.add_option("-i", "--interval", type="int", dest="interval", default=60, help="Time between state changes. Default is 60 seconds.")
    parser.add_option("-s", "--trace-sip", action="store_true", dest="trace_sip", default=False, help="Dump the raw contents of incoming and outgoing SIP messages (disabled by default).")
    parser.add_option("-j", "--trace-pjsip", action="store_true", dest="trace_pjsip", default=False, help="Print PJSIP logging output (disabled by default).")
    parser.add_option("-n", "--trace-notifications", action="store_true", dest="trace_notifications", default=False, help="Print all notifications (disabled by default).")
    options, args = parser.parse_args()

    try:
        application = AutoPublicationApplication(options.account_name, options.interval, options.trace_sip, options.trace_pjsip, options.trace_notifications)
        return_code = application.run()
    except RuntimeError, e:
        print "Error: %s" % str(e)
        sys.exit(1)
    except SIPCoreError, e:
        print "Error: %s" % str(e)
        sys.exit(1)
    else:
        sys.exit(return_code)


