import os
import sqlite3
import json
import csv
import asyncio
import time
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional, Any
from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, User, PeerChannel
from telethon.errors import FloodWaitError, RPCError
import aiohttp
import sys
from pathlib import Path

def display_ascii_art():
    WHITE = "\033[97m"
    RESET = "\033[0m"
    
    art = r"""
___________________  _________
\__    ___/  _____/ /   _____/
  |    | /   \  ___ \_____  \ 
  |    | \    \_\  \/        \
  |____|  \______  /_______  /
                 \/        \/
    """
    
    print(WHITE + art + RESET)

@dataclass
class MessageData:
    message_id: int
    date: str
    sender_id: int
    first_name: Optional[str]
    last_name: Optional[str]
    username: Optional[str]
    message: str
    media_type: Optional[str]
    media_path: Optional[str]
    reply_to: Optional[int]

class OptimizedTelegramScraper:
    def __init__(self):
        self.STATE_FILE = 'state.json'
        self.state = self.load_state()
        self.client = None
        self.continuous_scraping_active = False
        self.max_concurrent_downloads = 3
        self.batch_size = 100
        self.state_save_interval = 50
        self.db_connections = {}
        
    def load_state(self) -> Dict[str, Any]:
        if os.path.exists(self.STATE_FILE):
            with open(self.STATE_FILE, 'r') as f:
                return json.load(f)
        return {
            'api_id': None,
            'api_hash': None,
            'phone': None,
            'channels': {},
            'scrape_media': True,
        }

    def save_state(self):
        with open(self.STATE_FILE, 'w') as f:
            json.dump(self.state, f)

    def get_db_connection(self, channel: str) -> sqlite3.Connection:
        if channel not in self.db_connections:
            channel_dir = Path(os.getcwd()) / channel
            channel_dir.mkdir(exist_ok=True)
            
            db_file = channel_dir / f'{channel}.db'
            conn = sqlite3.connect(str(db_file), check_same_thread=False)
            conn.execute('''CREATE TABLE IF NOT EXISTS messages
                          (id INTEGER PRIMARY KEY, message_id INTEGER UNIQUE, date TEXT, 
                           sender_id INTEGER, first_name TEXT, last_name TEXT, username TEXT, 
                           message TEXT, media_type TEXT, media_path TEXT, reply_to INTEGER)''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_message_id ON messages(message_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_date ON messages(date)')
            conn.commit()
            self.db_connections[channel] = conn
        
        return self.db_connections[channel]

    def close_db_connections(self):
        for conn in self.db_connections.values():
            conn.close()
        self.db_connections.clear()

    def batch_insert_messages(self, channel: str, messages: List[MessageData]):
        if not messages:
            return
            
        conn = self.get_db_connection(channel)
        data = [(msg.message_id, msg.date, msg.sender_id, msg.first_name, 
                msg.last_name, msg.username, msg.message, msg.media_type, 
                msg.media_path, msg.reply_to) for msg in messages]
        
        conn.executemany('''INSERT OR IGNORE INTO messages 
                           (message_id, date, sender_id, first_name, last_name, username, 
                            message, media_type, media_path, reply_to)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', data)
        conn.commit()

    async def download_media_with_semaphore(self, semaphore: asyncio.Semaphore, 
                                          channel: str, message) -> Optional[str]:
        async with semaphore:
            return await self.download_media(channel, message)

    async def download_media(self, channel: str, message) -> Optional[str]:
        if not message.media or not self.state['scrape_media']:
            return None

        channel_dir = Path(os.getcwd()) / channel
        media_folder = channel_dir / 'media'
        media_folder.mkdir(exist_ok=True)
        
        media_file_name = None
        if isinstance(message.media, MessageMediaPhoto):
            media_file_name = getattr(message.file, 'name', None) or f"{message.id}.jpg"
        elif isinstance(message.media, MessageMediaDocument):
            ext = getattr(message.file, 'ext', 'bin') if message.file else 'bin'
            media_file_name = getattr(message.file, 'name', None) or f"{message.id}.{ext}"
        
        if not media_file_name:
            return None
        
        media_path = media_folder / media_file_name
        
        if media_path.exists():
            return str(media_path)

        max_retries = 3
        for attempt in range(max_retries):
            try:
                downloaded_path = await message.download_media(file=str(media_folder))
                if downloaded_path:
                    return downloaded_path
                break
            except FloodWaitError as e:
                if attempt < max_retries - 1:
                    print(f"Rate limited. Waiting {e.seconds} seconds...")
                    await asyncio.sleep(e.seconds)
                else:
                    print(f"Failed to download media for message {message.id} after rate limit")
                    return None
            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"Download failed for message {message.id}, retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    print(f"Failed to download media for message {message.id}: {e}")
                    return None
        
        return None

    async def update_media_path(self, channel: str, message_id: int, media_path: str):
        conn = self.get_db_connection(channel)
        conn.execute('UPDATE messages SET media_path = ? WHERE message_id = ?', 
                    (media_path, message_id))
        conn.commit()

    async def scrape_channel(self, channel: str, offset_id: int):
        try:
            if channel.startswith('-'):
                entity = await self.client.get_entity(PeerChannel(int(channel)))
            else:
                entity = await self.client.get_entity(channel)

            result = await self.client.get_messages(entity, offset_id=offset_id, reverse=True, limit=0)
            total_messages = result.total

            if total_messages == 0:
                print(f"No messages found in channel {channel}.")
                return

            print(f"Found {total_messages} messages in channel {channel}")

            message_batch = []
            media_download_tasks = []
            processed_messages = 0
            last_message_id = offset_id

            download_semaphore = asyncio.Semaphore(self.max_concurrent_downloads)

            async for message in self.client.iter_messages(entity, offset_id=offset_id, reverse=True):
                try:
                    sender = await message.get_sender()
                    
                    msg_data = MessageData(
                        message_id=message.id,
                        date=message.date.strftime('%Y-%m-%d %H:%M:%S'),
                        sender_id=message.sender_id,
                        first_name=getattr(sender, 'first_name', None) if isinstance(sender, User) else None,
                        last_name=getattr(sender, 'last_name', None) if isinstance(sender, User) else None,
                        username=getattr(sender, 'username', None) if isinstance(sender, User) else None,
                        message=message.message or '',
                        media_type=message.media.__class__.__name__ if message.media else None,
                        media_path=None,
                        reply_to=message.reply_to_msg_id if message.reply_to else None
                    )
                    
                    message_batch.append(msg_data)

                    if self.state['scrape_media'] and message.media:
                        task = asyncio.create_task(
                            self.download_media_with_semaphore(download_semaphore, channel, message)
                        )
                        media_download_tasks.append((message.id, task))

                    last_message_id = message.id
                    processed_messages += 1

                    if len(message_batch) >= self.batch_size:
                        self.batch_insert_messages(channel, message_batch)
                        message_batch.clear()

                    if processed_messages % self.state_save_interval == 0:
                        self.state['channels'][channel] = last_message_id
                        self.save_state()

                    progress = (processed_messages / total_messages) * 100
                    sys.stdout.write(f"\rScraping {channel}: {progress:.1f}% ({processed_messages}/{total_messages})")
                    sys.stdout.flush()

                except Exception as e:
                    print(f"Error processing message {message.id}: {e}")

            if message_batch:
                self.batch_insert_messages(channel, message_batch)

            if media_download_tasks:
                print(f"\nWaiting for {len(media_download_tasks)} media downloads to complete...")
                for message_id, task in media_download_tasks:
                    try:
                        media_path = await task
                        if media_path:
                            await self.update_media_path(channel, message_id, media_path)
                    except Exception as e:
                        print(f"Error in media download for message {message_id}: {e}")

            self.state['channels'][channel] = last_message_id
            self.save_state()
            
            print(f"\nCompleted scraping channel {channel}")

        except Exception as e:
            print(f"Error with channel {channel}: {e}")

    async def rescrape_media(self, channel: str):
        conn = self.get_db_connection(channel)
        cursor = conn.cursor()
        cursor.execute('SELECT message_id FROM messages WHERE media_type IS NOT NULL AND media_path IS NULL')
        message_ids = [row[0] for row in cursor.fetchall()]

        if not message_ids:
            print(f"No media files to reprocess for channel {channel}.")
            return

        print(f"Reprocessing {len(message_ids)} media files for channel {channel}")

        try:
            entity = await self.client.get_entity(PeerChannel(int(channel)))
        except Exception as e:
            print(f"Error getting entity for channel {channel}: {e}")
            return

        download_semaphore = asyncio.Semaphore(self.max_concurrent_downloads)
        tasks = []

        batch_size = 50
        for i in range(0, len(message_ids), batch_size):
            batch_ids = message_ids[i:i + batch_size]
            
            try:
                messages = await self.client.get_messages(entity, ids=batch_ids)
                
                for message in messages:
                    if message and message.media:
                        task = asyncio.create_task(
                            self.download_media_with_semaphore(download_semaphore, channel, message)
                        )
                        tasks.append((message.id, task))

                for message_id, task in tasks[-len([m for m in messages if m and m.media]):]:
                    try:
                        media_path = await task
                        if media_path:
                            await self.update_media_path(channel, message_id, media_path)
                    except Exception as e:
                        print(f"Error downloading media for message {message_id}: {e}")

                progress = min(100, (i + batch_size) / len(message_ids) * 100)
                sys.stdout.write(f"\rReprocessing media: {progress:.1f}%")
                sys.stdout.flush()

            except Exception as e:
                print(f"Error processing batch starting at {i}: {e}")

        print(f"\nCompleted media reprocessing for channel {channel}")

    async def continuous_scraping(self):
        self.continuous_scraping_active = True
        
        try:
            while self.continuous_scraping_active:
                start_time = time.time()
                
                for channel in self.state['channels']:
                    if not self.continuous_scraping_active:
                        break
                        
                    print(f"\nChecking for new messages in channel: {channel}")
                    await self.scrape_channel(channel, self.state['channels'][channel])
                
                elapsed = time.time() - start_time
                sleep_time = max(0, 60 - elapsed)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
                    
        except asyncio.CancelledError:
            print("Continuous scraping stopped.")
        finally:
            self.continuous_scraping_active = False

    def export_to_csv_optimized(self, channel: str):
        conn = self.get_db_connection(channel)
        csv_file = Path(channel) / f'{channel}.csv'
        
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM messages ORDER BY date')
        
        columns = [description[0] for description in cursor.description]
        
        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            
            while True:
                rows = cursor.fetchmany(1000)
                if not rows:
                    break
                writer.writerows(rows)

    def export_to_json_optimized(self, channel: str):
        conn = self.get_db_connection(channel)
        json_file = Path(channel) / f'{channel}.json'
        
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM messages ORDER BY date')
        columns = [description[0] for description in cursor.description]
        
        with open(json_file, 'w', encoding='utf-8') as f:
            f.write('[\n')
            first_row = True
            
            while True:
                rows = cursor.fetchmany(1000)
                if not rows:
                    break
                
                for row in rows:
                    if not first_row:
                        f.write(',\n')
                    else:
                        first_row = False
                    
                    data = dict(zip(columns, row))
                    json.dump(data, f, ensure_ascii=False, indent=2)
            
            f.write('\n]')

    async def export_data(self):
        for channel in self.state['channels']:
            print(f"Exporting data for channel {channel}...")
            self.export_to_csv_optimized(channel)
            self.export_to_json_optimized(channel)
            print(f"Completed export for channel {channel}")

    async def view_channels(self):
        if not self.state['channels']:
            print("No channels to view.")
            return
        
        print("\nCurrent channels:")
        for channel, last_id in self.state['channels'].items():
            try:
                conn = self.get_db_connection(channel)
                cursor = conn.cursor()
                cursor.execute('SELECT COUNT(*) FROM messages')
                count = cursor.fetchone()[0]
                print(f"Channel ID: {channel}, Last Message ID: {last_id}, Messages: {count}")
            except:
                print(f"Channel ID: {channel}, Last Message ID: {last_id}")

    async def list_channels(self):
        try:
            print("\nList of channels joined by account:")
            async for dialog in self.client.iter_dialogs():
                if dialog.id != 777000:
                    print(f"* {dialog.title} (id: {dialog.id})")
        except Exception as e:
            print(f"Error listing channels: {e}")

    async def initialize_client(self):
        if not all([self.state['api_id'], self.state['api_hash'], self.state['phone']]):
            self.state['api_id'] = int(input("Enter your API ID: "))
            self.state['api_hash'] = input("Enter your API Hash: ")
            self.state['phone'] = input("Enter your phone number: ")
            self.save_state()

        self.client = TelegramClient('session', self.state['api_id'], self.state['api_hash'])
        await self.client.start()

    async def manage_channels(self):
        while True:
            print("\nMenu:")
            print("[A] Add new channel")
            print("[R] Remove channel")
            print("[S] Scrape all channels")
            print("[M] Toggle media scraping (currently {})".format(
                "enabled" if self.state['scrape_media'] else "disabled"))
            print("[C] Continuous scraping")
            print("[E] Export data")
            print("[V] View saved channels")
            print("[L] List account channels")
            print("[Q] Quit")

            choice = input("Enter your choice: ").lower()
            
            match choice:
                case 'a':
                    channel = input("Enter channel ID: ")
                    self.state['channels'][channel] = 0
                    self.save_state()
                    print(f"Added channel {channel}.")
                    
                case 'r':
                    channel = input("Enter channel ID to remove: ")
                    if channel in self.state['channels']:
                        del self.state['channels'][channel]
                        self.save_state()
                        print(f"Removed channel {channel}.")
                    else:
                        print(f"Channel {channel} not found.")
                        
                case 's':
                    for channel in self.state['channels']:
                        await self.scrape_channel(channel, self.state['channels'][channel])
                        
                case 'm':
                    self.state['scrape_media'] = not self.state['scrape_media']
                    self.save_state()
                    print(f"Media scraping {'enabled' if self.state['scrape_media'] else 'disabled'}.")
                    
                case 'c':
                    task = asyncio.create_task(self.continuous_scraping())
                    print("Continuous scraping started. Press Ctrl+C to stop.")
                    try:
                        await asyncio.sleep(float('inf'))
                    except KeyboardInterrupt:
                        self.continuous_scraping_active = False
                        task.cancel()
                        print("\nStopping continuous scraping...")
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                            
                case 'e':
                    await self.export_data()
                    
                case 'v':
                    await self.view_channels()
                    
                case 'l':
                    await self.list_channels()
                    
                case 'q':
                    print("Quitting...")
                    self.close_db_connections()
                    await self.client.disconnect()
                    sys.exit()
                    
                case _:
                    print("Invalid option.")

    async def run(self):
        display_ascii_art()
        await self.initialize_client()
        try:
            await self.manage_channels()
        finally:
            self.close_db_connections()
            if self.client:
                await self.client.disconnect()

async def main():
    scraper = OptimizedTelegramScraper()
    await scraper.run()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProgram interrupted. Exiting...")
        sys.exit()