from zope.interface import implements
from application.notification import IObserver

class NotificationHandler(object):
    implements(IObserver)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, None)
        if handler is not None:
            handler(notification.sender, notification.data)