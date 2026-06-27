"""
Outbound message formatting and transport: splitting long replies into
radio-sized chunks, dropping stale queued messages, and the send/ack
loops for direct and channel replies. No game logic lives here.
"""

import asyncio
import textwrap
import time

from db import log_message
from meshcore import EventType


MAX_MSG_LEN = 130  # hard limit enforced by the meshcore radio/app


# Messages older than this (based on the sender's own sender_timestamp,
# not arrival time) are ignored. This catches commands that queued up on
# the radio while the app was disconnected and all arrive in a burst once
# it reconnects -- without this, a stale "move" or other command would
# get acted on as if it just happened.
MAX_MESSAGE_AGE_SECONDS = 120


def _prepare_lines(text, limit):
    """Split text into lines, word-wrapping any line that's too long on its own."""
    raw_lines = text.split("\n")
    lines = []
    for line in raw_lines:
        if len(line) <= limit:
            lines.append(line)
        else:
            lines.extend(textwrap.wrap(line, width=limit) or [""])
    return lines


def _pack_lines(lines, limit):
    """Greedily join lines with '\\n', keeping each resulting chunk <= limit."""
    chunks = []
    current = ""
    for line in lines:
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks or [""]


def chunk_message(text, limit=MAX_MSG_LEN):
    """
    Split text into one or more chunks that each fit within `limit` chars,
    preserving newlines for readability. Any single line longer than the
    limit on its own gets word-wrapped as a fallback. If more than one
    chunk is needed, each is prefixed with "(i/n) " so the recipient can
    tell a reply was split — the limit used for wrapping/packing is
    re-derived each pass so the prefix never pushes a chunk over `limit`.
    """
    chunks = _pack_lines(_prepare_lines(text, limit), limit)
    if len(chunks) <= 1:
        return chunks

    # Reserve room for the "(i/n) " prefix, then redo wrapping/packing at
    # the reduced width. Do a second pass in case digit count of n changes
    # after the first pass (e.g. 9 -> 10 chunks).
    n = len(chunks)
    prefix_width = len(f"({n}/{n}) ")
    reduced_limit = max(10, limit - prefix_width)
    chunks = _pack_lines(_prepare_lines(text, reduced_limit), reduced_limit)
    n2 = len(chunks)
    if n2 != n:
        prefix_width = len(f"({n2}/{n2}) ")
        reduced_limit = max(10, limit - prefix_width)
        chunks = _pack_lines(_prepare_lines(text, reduced_limit), reduced_limit)
        n2 = len(chunks)

    return [f"({i + 1}/{n2}) {c}" for i, c in enumerate(chunks)]


def is_stale_message(payload, max_age=MAX_MESSAGE_AGE_SECONDS):
    """
    True if payload's sender_timestamp is older than max_age seconds.
    sender_timestamp is set by the sender's radio when the message was
    originally sent, not when our app received it -- so this catches
    messages that sat queued on the radio (e.g. while the app was
    disconnected) and all arrived in a burst once it reconnected.
    Messages without a sender_timestamp are never treated as stale.
    """
    sender_timestamp = payload.get("sender_timestamp")
    if sender_timestamp is None:
        return False
    return (time.time() - sender_timestamp) > max_age



    """Per-message context passed to every command handler."""
    def __init__(self, mc, pubkey, sender, player):
        self.mc = mc
        self.pubkey = pubkey
        self.sender = sender
        self.player = player


async def send_reply(mc, pubkey, sender, text):
    """
    Send each chunk and wait for the recipient's radio to actually
    acknowledge it (send_msg_with_retry blocks until ACK or gives up after
    its own retries) before sending the next chunk. This replaces a fixed
    time delay with a real delivery confirmation, which is only possible
    for direct messages -- channel broadcasts have no per-recipient ACK.
    """
    for chunk in chunk_message(text):
        result = await mc.commands.send_msg_with_retry(pubkey, chunk)
        if result is None:
            print(f"  Error sending reply (no ack received): {chunk}")
            return
        print(f"  Reply sent + acked: {chunk}")
        log_message("tx", pubkey, sender, chunk)


async def send_channel_reply(mc, channel_idx, text):
    """Broadcast a reply to everyone on the given channel (not a private DM)."""
    chunks = chunk_message(text)
    for i, chunk in enumerate(chunks):
        result = await mc.commands.send_chan_msg(channel_idx, chunk)
        if result.type == EventType.ERROR:
            print(f"  Error sending channel reply: {result.payload}")
            return
        print(f"  Channel reply sent OK: {chunk}")
        log_message("tx", f"chan{channel_idx}", "channel", chunk)
        if len(chunks) > 1 and i < len(chunks) - 1:
            await asyncio.sleep(0.1)
