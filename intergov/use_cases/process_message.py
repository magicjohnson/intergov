import os

from intergov.conf import env
from intergov.domain.wire_protocols.generic_discrete import Message
from intergov.loggers import logging  # NOQA
from intergov.monitoring import statsd_timer

logger = logging.getLogger(__name__)

ENV_SEND_LOOPBACK_MESSAGES = str(
    os.environ.get('SEND_LOOPBACK_MESSAGES', None)
).lower() == "true"


class ProcessMessageUseCase:
    """
    Used by the message processing background worker.

    Gets one message from the channel inbox
    and does number of things with it.

    * dispatch document retreval job
      (if the message is from a foreign source)
    * dispatch message sending task to channel-outbox
      (if the message is from a domestic source)
    * ensure the message is stored in the message lake
    * ensure the access control lists are updated
      for this message
    * dispatch any WebSub events required for this message

    Note: the inbound message may have come from
    one of two sources: it may be a message from within
    this country, or it may be a message sent from another country.
    This use-case works with either message,
    however it needs to know which country it is working as
    to get the logic right
    (that is why it takes a country parameter
    when it is instantiated).
    """

    def __init__(
            self,
            country,
            bc_inbox_repo,
            message_lake_repo,
            object_acl_repo,
            object_retreval_repo,
            notifications_repo,  # TODO: rename to notifications_repo
            blockchain_outbox_repo):
        self.country = country
        self.bc_inbox_repo = bc_inbox_repo
        self.message_lake_repo = message_lake_repo
        self.object_acl_repo = object_acl_repo
        self.object_retreval_repo = object_retreval_repo
        self.notifications_repo = notifications_repo
        self.blockchain_outbox_repo = blockchain_outbox_repo

    def execute(self):
        # Get the message from the bc_inbox_repo (which is a events queue)
        fetched_bc_inbox = self.bc_inbox_repo.get()
        if not fetched_bc_inbox:
            return None
        (queue_message_id, message) = fetched_bc_inbox
        return self.process(queue_message_id, message)

    @statsd_timer("usecase.ProcessMessageUseCase.process")
    def process(self, queue_message_id, message):
        # let it be procssed
        logger.info("Received message to process: %s", message)

        # TODO: if something is fine and something is failed then first
        # steps will be done again
        # fine for object storage but not for queues
        try:
            ml_OK = self.message_lake_repo.post(message)
        except Exception as e:
            logger.exception(e)
            ml_OK = False
        try:
            acl_OK = self.object_acl_repo.post(message)
        except Exception as e:
            logger.exception(e)
            acl_OK = False

        try:
            # we delay it a little to make sure the message has got to the repo
            # and remove status because notifications don't need it
            message_without_status = Message.from_dict(
                message.to_dict(exclude=['status'])
            )
            # fat ping for ones who understand
            pub_OK = self.notifications_repo.post(
                message_without_status,
                delay_seconds=3
            )
            # light ping for ones who want everything
            self.notifications_repo.post_job(
                {
                    "predicate": f'message.{message.sender_ref}.received',
                    "sender_ref": f"{message.sender}:{message.sender_ref}"
                }
            )
        except Exception as e:
            logger.exception(e)
            pub_OK = False

        # blockchain part - pass the message to the blockchain worker
        # so it can be shared to the foreign parties
        outbox_OK = True
        ret_OK = True
        if str(message.sender) == str(self.country) and message.status == 'pending':
            # our jurisdiction -> remote
            logger.info("Sending message to the channels: %s", message.subject)
            try:
                outbox_OK = self.blockchain_outbox_repo.post(message)
            except Exception as e:
                logger.exception(e)
                outbox_OK = False
        elif str(message.sender) != str(self.country) and message.status == 'received':
            # Incoming message from remote juridsiction
            # might need to download remote documents using the
            # Documents Spider
            logger.info(
                "Received message from remote jurisdiction %s with subject %s",
                message.sender,
                message.subject
            )
            logger.info(
                "Scheduling download remote documents for: %s", message.subject
            )
            try:
                ret_OK = self.object_retreval_repo.post_job({
                    "action": "download-object",
                    "sender": message.sender,
                    "object": message.obj
                })
            except Exception as e:
                logger.exception(e)
                ret_OK = False
        else:
            # strange situation
            logger.warning(
                "Message sender is %s and we are %s and the status is %s - strange",
                message.sender,
                self.country,
                message.status
            )
            return False

        if ml_OK and acl_OK and ret_OK and pub_OK and outbox_OK:
            self.bc_inbox_repo.delete(queue_message_id)
            return True
        else:
            logger.error("Task processing failed, will try again later")
            # what TODO with the failed ones?
            # the problem is the fact that we have submitted message
            # to some repos and some other failed,
            # which means we must retry just failed submissions
            # and it may introduce a tricky state when some external message
            # processors will get info from the one source and won't get it
            # from the another. They should wait then.
            return False
