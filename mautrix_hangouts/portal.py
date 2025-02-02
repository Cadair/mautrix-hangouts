# mautrix-hangouts - A Matrix-Hangouts puppeting bridge
# Copyright (C) 2019 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from typing import Dict, Deque, Optional, Tuple, Union, Set, Iterator, TYPE_CHECKING
from collections import deque
import asyncio
import logging
import io
import cgi

from hangups import hangouts_pb2 as hangouts, ChatMessageEvent
from hangups.user import User as HangoutsUser
from hangups.conversation import Conversation as HangoutsChat
from mautrix.types import (RoomID, MessageEventContent, EventID, MessageType,
                           TextMessageEventContent, MediaMessageEventContent, Membership)
from mautrix.appservice import AppService, IntentAPI
from mautrix.errors import MatrixError

from .config import Config
from .db import Portal as DBPortal, Message as DBMessage
from . import puppet as p, user as u

if TYPE_CHECKING:
    from .context import Context

config: Config


class FakeLock:
    async def __aenter__(self) -> None:
        pass

    async def __aexit__(self, exc_type, exc, tb) -> None:
        pass


class Portal:
    az: AppService
    loop: asyncio.AbstractEventLoop
    log: logging.Logger = logging.getLogger("mau.portal")
    invite_own_puppet_to_pm: bool = False
    by_mxid: Dict[RoomID, 'Portal'] = {}
    by_gid: Dict[Tuple[str, str], 'Portal'] = {}

    gid: str
    receiver: Optional[str]
    conv_type: int
    other_user_id: Optional[str]
    mxid: Optional[RoomID]

    name: str

    _db_instance: DBPortal

    _main_intent: Optional[IntentAPI]
    _create_room_lock: asyncio.Lock
    _last_bridged_mxid: Optional[EventID]
    _dedup: Deque[Tuple[str, str]]
    _send_locks: Dict[str, asyncio.Lock]
    _noop_lock: FakeLock = FakeLock()
    _typing: Set['u.User']

    def __init__(self, gid: str, receiver: str, conv_type: int, other_user_id: Optional[str] = None,
                 mxid: Optional[RoomID] = None, name: str = "",
                 db_instance: Optional[DBPortal] = None) -> None:
        self.gid = gid
        self.receiver = receiver
        if not receiver:
            raise ValueError("Receiver not given")
        self.conv_type = conv_type
        self.other_user_id = other_user_id
        self.mxid = mxid

        self.name = name

        self._db_instance = db_instance

        self._main_intent = None
        self._create_room_lock = asyncio.Lock()
        self._last_bridged_mxid = None
        self._dedup = deque(maxlen=100)
        self._send_locks = {}
        self._typing = set()

        self.log = self.log.getChild(self.gid_log)

        self.by_gid[self.full_gid] = self
        if self.mxid:
            self.by_mxid[self.mxid] = self

    @property
    def full_gid(self) -> Tuple[str, str]:
        return self.gid, self.receiver

    @property
    def gid_log(self) -> str:
        if self.conv_type == hangouts.CONVERSATION_TYPE_ONE_TO_ONE:
            return f"{self.gid}-{self.receiver}"
        return self.gid

    # region DB conversion

    @property
    def db_instance(self) -> DBPortal:
        if not self._db_instance:
            self._db_instance = DBPortal(gid=self.gid, receiver=self.receiver,
                                         conv_type=self.conv_type, other_user_id=self.other_user_id,
                                         mxid=self.mxid, name=self.name)
        return self._db_instance

    @classmethod
    def from_db(cls, db_portal: DBPortal) -> 'Portal':
        return Portal(gid=db_portal.gid, receiver=db_portal.receiver, conv_type=db_portal.conv_type,
                      other_user_id=db_portal.other_user_id, mxid=db_portal.mxid,
                      name=db_portal.name, db_instance=db_portal)

    def save(self) -> None:
        self.db_instance.edit(other_user_id=self.other_user_id, mxid=self.mxid, name=self.name)

    def delete(self) -> None:
        self.by_gid.pop(self.full_gid, None)
        self.by_mxid.pop(self.mxid, None)
        if self._db_instance:
            self._db_instance.delete()

    # endregion
    # region Properties

    @property
    def is_direct(self) -> bool:
        return self.conv_type == hangouts.CONVERSATION_TYPE_ONE_TO_ONE

    @property
    def main_intent(self) -> IntentAPI:
        if not self._main_intent:
            if self.is_direct:
                if not self.other_user_id:
                    raise ValueError("Portal.other_user_id not set for private chat")
                self._main_intent = p.Puppet.get_by_gid(self.other_user_id).default_mxid_intent
            else:
                self._main_intent = self.az.intent
        return self._main_intent

    # endregion
    # region Chat info updating

    async def update_info(self, source: Optional['u.User'] = None,
                          info: Optional[HangoutsChat] = None) -> HangoutsChat:
        if not info:
            info = source.chats.get(self.gid)
            # info = await source.client.get_conversation(hangouts.GetConversationRequest(
            #     request_header=source.client.get_request_header(),
            #     conversation_spec=hangouts.ConversationSpec(
            #         conversation_id=hangouts.ConversationId(id=self.gid),
            #     ),
            #     include_event=False,
            # ))
        changed = any(await asyncio.gather(self._update_name(info.name),
                                           self._update_participants(source, info),
                                           loop=self.loop))
        if changed:
            self.save()
        return info

    async def _update_name(self, name: str) -> bool:
        if self.name != name:
            self.name = name
            if self.mxid and not self.is_direct:
                await self.main_intent.set_room_name(self.mxid, self.name)
            return True
        return False

    async def _update_participants(self, source: 'u.User', info: HangoutsChat) -> None:
        if not self.mxid:
            return
        users = info.users
        if self.is_direct:
            users = [user for user in users if user.id_.gaia_id != source.gid]
            self.other_user_id = users[0].id_.gaia_id
        puppets: Dict[HangoutsUser, p.Puppet] = {user: p.Puppet.get_by_gid(user.id_.gaia_id)
                                                 for user in users}
        await asyncio.gather(*[puppet.update_info(source=source, info=user)
                               for user, puppet in puppets.items()])
        await asyncio.gather(*[puppet.intent_for(self).ensure_joined(self.mxid)
                               for puppet in puppets.values()])

    # endregion
    # region Matrix room creation

    async def _update_matrix_room(self, source: 'u.User',
                                  info: Optional[HangoutsChat] = None) -> None:
        await self.main_intent.invite_user(self.mxid, source.mxid, check_cache=True)
        puppet = p.Puppet.get_by_custom_mxid(source.mxid)
        if puppet:
            await puppet.intent.ensure_joined(self.mxid)
        await self.update_info(source, info)

    async def create_matrix_room(self, source: 'u.User', info: Optional[HangoutsChat] = None
                                 ) -> RoomID:
        if self.mxid:
            try:
                await self._update_matrix_room(source, info)
            except Exception:
                self.log.exception("Failed to update portal")
            return self.mxid
        async with self._create_room_lock:
            try:
                return await self._create_matrix_room(source, info)
            except Exception:
                self.log.exception("Failed to create portal")

    async def _create_matrix_room(self, source: 'u.User', info: Optional[HangoutsChat] = None
                                  ) -> RoomID:
        if self.mxid:
            await self._update_matrix_room(source, info)
            return self.mxid

        info = await self.update_info(source=source, info=info)
        self.log.debug("Creating Matrix room")
        name: Optional[str] = None
        if self.is_direct:
            users = [user for user in info.users if user.id_.gaia_id != source.gid]
            self.other_user_id = users[0].id_.gaia_id
            puppet = p.Puppet.get_by_gid(self.other_user_id)
            await puppet.update_info(source=source, info=info.get_user(self.other_user_id))
        else:
            name = self.name
        self.mxid = await self.main_intent.create_room(name=name, is_direct=self.is_direct,
                                                       invitees=[source.mxid])
        self.save()
        self.log.debug(f"Matrix room created: {self.mxid}")
        if not self.mxid:
            raise Exception("Failed to create room: no mxid required")
        self.by_mxid[self.mxid] = self
        await self._update_participants(source, info)
        puppet = p.Puppet.get_by_custom_mxid(source.mxid)
        if puppet:
            await puppet.intent.ensure_joined(self.mxid)
        await source._community_helper.add_room(source._community_id, self.mxid)
        return self.mxid

    # endregion
    # region Matrix room cleanup

    @staticmethod
    async def cleanup_room(intent: IntentAPI, room_id: RoomID, message: str = "Portal deleted",
                           puppets_only: bool = False) -> None:
        try:
            members = await intent.get_room_members(room_id)
        except MatrixError:
            members = []
        for user_id in members:
            puppet = p.Puppet.get_by_mxid(user_id, create=False)
            if user_id != intent.mxid and (not puppets_only or puppet):
                try:
                    if puppet:
                        await puppet.intent.leave_room(room_id)
                    else:
                        await intent.kick_user(room_id, user_id, message)
                except MatrixError:
                    pass
        try:
            await intent.leave_room(room_id)
        except MatrixError:
            pass

    async def unbridge(self) -> None:
        await self.cleanup_room(self.main_intent, self.mxid, "Room unbridged", puppets_only=True)
        self.delete()

    async def cleanup_and_delete(self) -> None:
        await self.cleanup_room(self.main_intent, self.mxid)
        self.delete()

    # endregion
    # region Matrix event handling

    def require_send_lock(self, user_id: str) -> asyncio.Lock:
        try:
            lock = self._send_locks[user_id]
        except KeyError:
            lock = asyncio.Lock()
            self._send_locks[user_id] = lock
        return lock

    def optional_send_lock(self, user_id: str) -> Union[asyncio.Lock, FakeLock]:
        try:
            return self._send_locks[user_id]
        except KeyError:
            pass
        return self._noop_lock

    async def handle_matrix_message(self, sender: 'u.User', message: MessageEventContent,
                                    event_id: EventID) -> None:
        puppet = p.Puppet.get_by_custom_mxid(sender.mxid)
        if puppet and message.get("net.maunium.hangouts.puppet", False):
            self.log.debug(f"Ignoring puppet-sent message by confirmed puppet user {sender.mxid}")
            return
        # TODO this probably isn't nice for bridging images, it really only needs to lock the
        #      actual message send call and dedup queue append.
        async with self.require_send_lock(sender.gid):
            if message.msgtype == MessageType.TEXT or message.msgtype == MessageType.NOTICE:
                gid = await self._handle_matrix_text(sender, message)
            elif message.msgtype == MessageType.EMOTE:
                gid = await self._handle_matrix_emote(sender, message)
            elif message.msgtype == MessageType.IMAGE:
                gid = await self._handle_matrix_image(sender, message)
            # elif message.msgtype == MessageType.LOCATION:
            #     gid = await self._handle_matrix_location(sender, message)
            else:
                self.log.warning(f"Unsupported msgtype in {message}")
                return
            if not gid:
                return
            self._dedup.appendleft(gid)
            DBMessage(mxid=event_id, mx_room=self.mxid, gid=gid, receiver=self.receiver,
                      index=0).insert()
            self._last_bridged_mxid = event_id

    async def _handle_matrix_text(self, sender: 'u.User', message: TextMessageEventContent) -> str:
        return await sender.send_text(self.gid, message.body)

    async def _handle_matrix_emote(self, sender: 'u.User', message: TextMessageEventContent) -> str:
        return await sender.send_emote(self.gid, message.body)

    async def _handle_matrix_image(self, sender: 'u.User',
                                   message: MediaMessageEventContent) -> str:
        data = await self.main_intent.download_media(message.url)
        image = await sender.client.upload_image(io.BytesIO(data), filename=message.body)
        return await sender.send_image(self.gid, image)

    #
    # async def _handle_matrix_location(self, sender: 'u.User',
    #                                   message: LocationMessageEventContent) -> str:
    #     pass

    async def handle_matrix_leave(self, user: 'u.User') -> None:
        if self.is_direct:
            self.log.info(f"{user.mxid} left private chat portal with {self.gid},"
                          " cleaning up and deleting...")
            await self.cleanup_and_delete()
        else:
            self.log.debug(f"{user.mxid} left portal to {self.gid}")

    async def handle_matrix_typing(self, users: Set['u.User']) -> None:
        stopped_typing = [user.set_typing(self.gid, False)
                          for user in self._typing - users]
        started_typing = [user.set_typing(self.gid, True)
                          for user in users - self._typing]
        self._typing = users
        await asyncio.gather(*stopped_typing, *started_typing, loop=self.loop)

    # endregion
    # region Hangouts event handling

    async def handle_hangouts_message(self, source: 'u.User', sender: 'p.Puppet',
                                      event: ChatMessageEvent) -> None:
        if self.is_direct and sender.gid == source.gid:
            if self.invite_own_puppet_to_pm:
                await self.main_intent.invite_user(self.mxid, sender.mxid)
            elif self.az.state_store.get_membership(self.mxid, sender.mxid) != Membership.JOIN:
                self.log.debug(f"Ignoring own message {event.id_} in private chat because own"
                               " puppet is not in room.")
                return
        async with self.optional_send_lock(sender.gid):
            if event.id_ in self._dedup:
                return
            self._dedup.appendleft(event.id_)
        if not self.mxid:
            await self.create_matrix_room(source)
        intent = sender.intent_for(self)

        event_id = None
        if event.attachments:
            self.log.debug(f"Attachments: {event.attachments}")
            self.log.debug("Processing attachments.")
            event_id = await self.process_hangouts_attachments(event, intent)
        # Just to fallback to text if something else hasn't worked.
        if not event_id:
            event_id = await intent.send_text(self.mxid, event.text)
        DBMessage(mxid=event_id, mx_room=self.mxid, gid=event.id_, receiver=self.receiver,
                  index=0).insert()

    async def _get_remote_bytes(self, url):
        async with self.az.http_session.request("GET", url) as resp:
            return await resp.read()

    async def process_hangouts_attachments(self, event: ChatMessageEvent, intent: IntentAPI)
        attachments_pb = event._event.chat_message.message_content.attachment

        if len(event.attachments) > 1:
            self.log.warning("Can't handle more that one attachment")
            return

        attachment = event.attachments[0]
        attachment_pb = attachments_pb[0]

        embed_item = attachment_pb.embed_item

        # Get the filename from the headers
        async with self.az.http_session.request("GET", attachment) as resp:
            value, params = cgi.parse_header(resp.headers["Content-Disposition"])
            filename = params.get('filename', attachment.split("/")[-1])

        # TODO: This also catches movies, but I can't work out how they present
        # differently to images
        if embed_item.type[0] == hangouts.ITEM_TYPE_PLUS_PHOTO:
            mxc_url = await intent.upload_media(await self._get_remote_bytes(attachment),
                                                filename=filename)
            return await intent.send_image(self.mxid, mxc_url, file_name=filename)

    async def handle_hangouts_typing(self, source: 'u.User', sender: 'p.Puppet', status: int
                                     ) -> None:
        if not self.mxid:
            return
        if ((self.is_direct and sender.gid == source.gid
             and self.az.state_store.get_membership(self.mxid, sender.mxid) != Membership.JOIN)):
            return
        await sender.intent_for(self).set_typing(self.mxid, status == hangouts.TYPING_TYPE_STARTED,
                                                 timeout=6000)

    # endregion
    # region Getters

    @classmethod
    def get_by_mxid(cls, mxid: RoomID) -> Optional['Portal']:
        try:
            return cls.by_mxid[mxid]
        except KeyError:
            pass

        db_portal = DBPortal.get_by_mxid(mxid)
        if db_portal:
            return cls.from_db(db_portal)

        return None

    @classmethod
    def get_by_gid(cls, gid: str, receiver: Optional[str] = None, conv_type: Optional[int] = None,
                   ) -> Optional['Portal']:
        if not receiver or (conv_type and conv_type != hangouts.CONVERSATION_TYPE_ONE_TO_ONE):
            receiver = gid
        try:
            return cls.by_gid[(gid, receiver)]
        except KeyError:
            pass

        db_portal = DBPortal.get_by_gid(gid, receiver)
        if db_portal:
            if not db_portal.receiver:
                cls.log.warning(f"Found DBPortal {gid} without receiver, setting to {receiver}")
                db_portal.edit(receiver=receiver)
            return cls.from_db(db_portal)

        if conv_type is not None:
            portal = cls(gid=gid, receiver=receiver, conv_type=conv_type)
            portal.db_instance.insert()
            return portal

        return None

    @classmethod
    def get_all_by_receiver(cls, receiver: str) -> Iterator['Portal']:
        for db_portal in DBPortal.get_all_by_receiver(receiver):
            try:
                yield cls.by_gid[(db_portal.gid, db_portal.receiver)]
            except KeyError:
                yield cls.from_db(db_portal)

    @classmethod
    def get_by_conversation(cls, conversation: HangoutsChat, receiver: str) -> Optional['Portal']:
        return cls.get_by_gid(conversation.id_, receiver, conversation._conversation.type)

    # endregion


def init(context: 'Context') -> None:
    global config
    Portal.az, config, Portal.loop = context.core
    Portal.invite_own_puppet_to_pm = config["bridge.invite_own_puppet_to_pm"]
