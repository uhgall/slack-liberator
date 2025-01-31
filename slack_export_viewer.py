import json
import os
import sys
import argparse
import urllib.request
import shutil
from datetime import datetime, timedelta, timezone, date
import hashlib
from typing import List, Dict, Set
import logging
import zipfile
import tempfile
from pathlib import Path
from functools import partial
from types import SimpleNamespace
from pydantic import BaseModel
import html

def setup_logging():
    """Configure logging for the application"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

def log(level, message, **kwargs):
    """
    Utility function for consistent logging
    
    Args:
        level (str): Log level ('debug', 'info', 'warning', 'error', 'critical')
        message (str): Message template with optional placeholders
        **kwargs: Values to fill placeholders in message
    """
    log_func = getattr(logging, level.lower())
    if kwargs:
        message = message.format(**kwargs)
    log_func(message)

class SlackExportViewer:
    def __init__(self, output_dir: str = "output", zip_path: str = None):
        setup_logging()  # Initialize logging
        self.output_dir = output_dir
        self.zip_path = zip_path
        self.temp_dir = None
        self.channels_data = {}
        self.users_data = {}  # Add users data storage
        self.processed_attachments: Set[str] = set()
        self.failed_downloads: Set[str] = set()
        self.channel_files: Dict[str, Dict[str, str]] = {}  # channel -> {file_id -> local_path}
        self.shown_images: Set[str] = set()  # Track which images we've shown inline
        self.logged_warnings = set()  # Track which warnings we've already logged
        
        if zip_path:
            self.setup_zip_environment()
    
    def setup_zip_environment(self):
        """Extract zip file to temporary directory"""
        self.temp_dir = tempfile.mkdtemp()
        log('info', 'Extracting zip file to temporary directory: {dir}', dir=self.temp_dir)
        
        try:
            with zipfile.ZipFile(self.zip_path, 'r') as zip_ref:
                zip_ref.extractall(self.temp_dir)
        except Exception as e:
            log('error', 'Failed to extract zip file: {error}', error=str(e))
            if self.temp_dir:
                shutil.rmtree(self.temp_dir)
            sys.exit(1)
    
    def get_data_path(self, path: str) -> str:
        """Get the correct path for data files whether using zip or direct files"""
        if self.zip_path:
            # Remove 'export_data/' prefix if it exists
            clean_path = path.replace('export_data/', '', 1)
            
            # Look for the file in the temp directory
            full_path = os.path.join(self.temp_dir, clean_path)
            if os.path.exists(full_path):
                return full_path
            
            # If not found, try looking for just the filename
            filename = os.path.basename(path)
            for root, _, files in os.walk(self.temp_dir):
                if filename in files:
                    return os.path.join(root, filename)
            
            return full_path  # Return the clean path even if not found
        return path

    def __del__(self):
        """Cleanup temporary directory if it exists"""
        if self.temp_dir and os.path.exists(self.temp_dir):
            log('info', f"Cleaning up temporary directory: {self.temp_dir}")
            shutil.rmtree(self.temp_dir)

    def load_channels(self, channels_file: str) -> None:
        """Load and parse the channels.json file"""
        try:
            with open(channels_file, 'r') as f:
                channels = json.load(f)
                self.channels_data = {c['name']: c for c in channels}
        except Exception as e:
            log('error', f"Failed to load channels file: {e}")
            sys.exit(1)

    def load_users(self, users_file: str) -> None:
        """Load and parse the users.json file"""
        try:
            with open(users_file, 'r') as f:
                users = json.load(f)
                self.users_data = {u['id']: u for u in users}
        except Exception as e:
            log('error', f"Failed to load users file: {e}")
            sys.exit(1)

    def get_username(self, user_id: str) -> str:
        """Get user's display name or real name"""
        if user_id not in self.users_data:
            return user_id
        
        user = self.users_data[user_id]
        profile = user.get('profile', {})
        
        # Try different name fields in order of preference
        return (profile.get('display_name') or 
                profile.get('real_name') or 
                user.get('name') or 
                user_id)

    def get_file_path(self, file_id: str, files_dir: str) -> str | None:
        """
        Find a file path given a file ID by checking for any file that starts with that ID.
        Returns None if no matching file is found.
        """
        if not os.path.exists(files_dir):
            return None
        
        for filename in os.listdir(files_dir):
            if filename.startswith(file_id):
                return os.path.join(files_dir, filename)
        return None

    def download_file(self, url: str, file_id: str, channel: str, original_name: str = None) -> tuple[str, bool]:
        """Download a file from Slack and return the local path and success status"""
        if not url or not (url.startswith('http://') or url.startswith('https://')):
            log('debug', 'Skipping invalid URL: {url}', url=url)
            return url, True
        
        # Only download from Slack domains
        if not ('slack.com' in url or 'slack-edge.com' in url):
            log('debug', 'Skipping non-Slack URL: {url}', url=url)
            return url, True
        
        try:
            # Initialize channel's file tracking if needed
            if channel not in self.channel_files:
                self.channel_files[channel] = {}
            
            # Determine filename and path FIRST
            if original_name:
                name, ext = os.path.splitext(original_name)
                if not ext:
                    ext = '.unknown'
                filename = f"{file_id}-{original_name}"
            else:
                ext = os.path.splitext(url)[1] or '.unknown'
                filename = f"{file_id}{ext}"
            
            # All files go in the files directory
            local_path = os.path.join(self.output_dir, channel, 'files')
            
            # Check if any file with this file_id prefix exists
            existing_file = self.get_file_path(file_id, local_path)
            if existing_file:
                log('debug', 'Found existing file with ID {file_id}: {path}', 
                    file_id=file_id, path=existing_file)
                self.channel_files[channel][file_id] = existing_file
                return existing_file, True
            
            # If no existing file found, proceed with download
            local_path = os.path.join(local_path, filename)
            
            # If we get here, we need to download the file
            log('debug', 'Downloading file: {file_id} ({url}) -> {path}', 
                file_id=file_id, url=url, path=local_path)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            
            # Add headers to mimic a browser request
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Referer': 'https://www.google.com'
            }
            
            try:
                # Create a Request object with headers
                req = urllib.request.Request(url, headers=headers)
                
                # Try to download with a timeout
                with urllib.request.urlopen(req, timeout=10) as response:
                    if response.status != 200:
                        raise urllib.error.URLError(f"HTTP {response.status}: {response.reason}")
                    
                    with open(local_path, 'wb') as out_file:
                        shutil.copyfileobj(response, out_file)
                
                # Verify the file was actually created and has content
                if not os.path.exists(local_path) or os.path.getsize(local_path) == 0:
                    raise Exception("File was not created or is empty")
                
                # Track this file
                self.channel_files[channel][file_id] = local_path
                
                log('debug', 'Successfully downloaded file: {file_id}', file_id=file_id)
                return local_path, True
                
            except urllib.error.HTTPError as e:
                log('error', 'HTTP error downloading {name} from {url}: {code} {reason}', 
                    name=original_name or file_id,
                    url=url,
                    code=e.code,
                    reason=e.reason)
                return None, False
                
            except urllib.error.URLError as e:
                log('error', 'URL error downloading {name} from {url}: {reason}', 
                    name=original_name or file_id,
                    url=url,
                    reason=str(e.reason))
                return None, False
                
            except TimeoutError:
                log('error', 'Timeout downloading {name} from {url}', 
                    name=original_name or file_id,
                    url=url)
                return None, False
                
        except Exception as e:
            log('error', 'Failed to download {name} from {url}: {error}', 
                name=original_name or file_id,
                url=url, 
                error=str(e))
            self.failed_downloads.add(url)
            return None, False

    def process_message(self, msg: Dict, channel: str) -> Dict:
        """Process a message and its files"""
        processed_msg = msg.copy()
        
        # Initialize tracking for this channel if needed
        if not hasattr(self, 'channel_missing_files'):
            self.channel_missing_files = {}
        if not hasattr(self, 'channel_downloaded_files'):
            self.channel_downloaded_files = {}
        
        if channel not in self.channel_missing_files:
            self.channel_missing_files[channel] = []
        if channel not in self.channel_downloaded_files:
            self.channel_downloaded_files[channel] = []
        
        # Collect all files from both files and attachments
        all_files = []
        
        # Add regular files
        if 'files' in processed_msg:
            all_files.extend((file_info, False) for file_info in processed_msg['files'])
        
        # Add files from attachments
        if 'attachments' in processed_msg:
            for attachment in processed_msg['attachments']:
                if 'files' in attachment:
                    all_files.extend((file_info, True) for file_info in attachment['files'])
        
        # Process all files uniformly
        processed_files = []
        for file_info, is_attachment in all_files:
            processed_file = file_info.copy()
            file_id = file_info.get('id', '')
            if not file_id and is_attachment:
                # For attachments without ID, create one from URL
                url = file_info.get('url_private', '')
                file_id = hashlib.md5(url.encode()).hexdigest()
                processed_file['id'] = file_id
            
            mode = file_info.get('mode', '')
            name = file_info.get('name', 'Unnamed file')
            
            # Check if file exists in the files directory
            files_dir = os.path.join(self.output_dir, channel, 'files')
            existing_path = self.get_file_path(file_id, files_dir)
            
            if existing_path:
                # File exists locally
                rel_path = os.path.relpath(existing_path, os.path.join(self.output_dir, channel))
                processed_file['local_path'] = rel_path
                processed_file['download_failed'] = False
                self.channel_downloaded_files[channel].append({
                    'timestamp': msg.get('ts', '0'),
                    'file_id': file_id,
                    'mode': 'exists'
                })
                processed_files.append(processed_file)
                continue
            
            # Handle missing or failed files
            url = file_info.get('url_private', '')
            if not url or mode in ['tombstone', 'hidden_by_limit']:
                self.channel_missing_files[channel].append({
                    'timestamp': msg.get('ts', '0'),
                    'file_id': file_id,
                    'mode': mode if mode else 'url_missing'
                })
                processed_file['download_failed'] = True
                processed_file['local_path'] = None
                processed_file['failure_reason'] = f'File {mode if mode else "URL missing"}'
                processed_files.append(processed_file)
                continue
            
            # Try to download the file
            local_path, success = self.download_file(url, file_id, channel, name)
            
            if success and local_path:
                self.channel_downloaded_files[channel].append({
                    'timestamp': msg.get('ts', '0'),
                    'file_id': file_id,
                    'mode': 'downloaded'
                })
                rel_path = os.path.relpath(local_path, os.path.join(self.output_dir, channel))
                processed_file['local_path'] = rel_path
                processed_file['download_failed'] = False
            else:
                self.channel_missing_files[channel].append({
                    'timestamp': msg.get('ts', '0'),
                    'file_id': file_id,
                    'mode': 'download_failed'
                })
                processed_file['download_failed'] = True
                processed_file['local_path'] = None
                processed_file['failure_reason'] = 'Download failed'
            
            processed_files.append(processed_file)
        
        # Update message with processed files
        if processed_files:
            processed_msg['files'] = processed_files
        
        return processed_msg

    def process_blocks(self, blocks: List[Dict]) -> str:
        """Process Slack blocks into HTML"""
        output = ""
        for block in blocks:
            if block['type'] == 'rich_text':
                for element in block['elements']:
                    if element['type'] == 'rich_text_section':
                        for item in element['elements']:
                            if item['type'] == 'text':
                                output += html.escape(item['text'])
                            elif item['type'] == 'link':
                                # Escape the text but not the URL
                                text = html.escape(item.get("text", item["url"]))
                                url = item["url"]  # URLs should not be escaped
                                output += f'<a href="{url}">{text}</a>'
                            elif item['type'] == 'emoji':
                                output += f':{item["name"]}:'
        return output

    def generate_channel_page(self, channel: str, messages: List[Dict]) -> str:
        """Generate HTML for a channel's messages"""
        # Group messages by thread first to get counts
        threads = {}  # thread_ts -> list of messages
        for msg in messages:
            thread_ts = msg.get('thread_ts')
            if thread_ts:
                if thread_ts not in threads:
                    threads[thread_ts] = []
                threads[thread_ts].append(msg)
        
        # Count actual threads (ones with replies)
        thread_count = sum(1 for ts, msgs in threads.items() 
                         if any(m['ts'] != ts for m in msgs))
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Slack Export - #{channel}</title>
            <style>
                body {{ 
                    font-family: Arial, sans-serif; 
                    margin: 20px; 
                }}
                .message {{ 
                    margin: 10px 0; 
                    padding: 10px; 
                    border-bottom: 1px solid #eee; 
                }}
                .timestamp {{ 
                    color: #666; 
                    font-size: 0.8em; 
                }}
                .user {{ 
                    font-weight: bold; 
                    color: #1264A3; 
                }}
                .attachment {{ 
                    margin: 10px 0; 
                }}
                .attachment img {{ 
                    max-width: 400px; 
                }}
                .failed-download {{ 
                    color: #666;
                    font-style: italic;
                }}
                nav {{ 
                    margin-bottom: 20px; 
                }}
                .thumbnail {{
                    max-width: 200px;
                    max-height: 200px;
                    object-fit: contain;
                    cursor: pointer;
                }}
                .thumbnail-link {{
                    display: inline-block;
                    text-decoration: none;
                }}
                .thread-toggle {{
                    color: #1264A3;
                    text-decoration: none;
                    font-size: 0.9em;
                    margin-top: 5px;
                    cursor: pointer;
                }}
                .thread-toggle:hover {{
                    text-decoration: underline;
                }}
                .thread-container {{
                    margin-left: 20px;
                    border-left: 2px solid #eee;
                    padding-left: 10px;
                    display: none;
                }}
                .thread-container.expanded {{
                    display: block;
                }}
                .thread-controls {{
                    margin-bottom: 20px;
                    padding: 10px;
                    background: #f8f8f8;
                    border-radius: 5px;
                }}
                .thread-controls a {{
                    color: #1264A3;
                    text-decoration: none;
                    margin-right: 20px;
                    cursor: pointer;
                }}
                .thread-controls a:hover {{
                    text-decoration: underline;
                }}
                .reply-count {{
                    color: #666;
                    font-size: 0.9em;
                }}
                .file {{
                    margin: 10px 0;
                }}
                
                .image-container {{
                    margin: 10px 0;
                    max-width: 800px;
                }}
                
                .message-image {{
                    max-width: 100%;
                    height: auto;
                    border-radius: 4px;
                    display: block;
                    margin-bottom: 8px;
                }}
                
                .image-caption {{
                    font-size: 0.9em;
                    color: #666;
                }}
                
                .image-caption a {{
                    color: #1264A3;
                    text-decoration: none;
                }}
                
                .image-caption a:hover {{
                    text-decoration: underline;
                }}
                
                .file-link {{
                    margin: 5px 0;
                }}
                
                .file-link a {{
                    color: #1264A3;
                    text-decoration: none;
                    display: inline-flex;
                    align-items: center;
                    padding: 6px 12px;
                    background: #f8f9fa;
                    border-radius: 4px;
                }}
                
                .file-link a:hover {{
                    background: #e9ecef;
                    text-decoration: none;
                }}
                
                .text {{
                    white-space: pre-line;  /* Preserve line breaks but not spaces */
                    word-wrap: break-word;  /* Break long words */
                    margin: 8px 0;
                }}
            </style>
            <script>
                function toggleThread(threadId) {{
                    const container = document.getElementById('thread-' + threadId);
                    container.classList.toggle('expanded');
                    
                    const toggle = document.getElementById('toggle-' + threadId);
                    const replies = toggle.getAttribute('data-replies');
                    if (container.classList.contains('expanded')) {{
                        toggle.textContent = 'Hide thread (' + replies + ' replies) ↑';
                    }} else {{
                        toggle.textContent = 'Show thread (' + replies + ' replies) ↓';
                    }}
                }}
                
                function expandAllThreads() {{
                    document.querySelectorAll('.thread-container').forEach(container => {{
                        container.classList.add('expanded');
                    }});
                    document.querySelectorAll('.thread-toggle').forEach(toggle => {{
                        const replies = toggle.getAttribute('data-replies');
                        toggle.textContent = 'Hide thread (' + replies + ' replies) ↑';
                    }});
                }}
                
                function collapseAllThreads() {{
                    document.querySelectorAll('.thread-container').forEach(container => {{
                        container.classList.remove('expanded');
                    }});
                    document.querySelectorAll('.thread-toggle').forEach(toggle => {{
                        const replies = toggle.getAttribute('data-replies');
                        toggle.textContent = 'Show thread (' + replies + ' replies) ↓';
                    }});
                }}
            </script>
        </head>
        <body>
            <nav>
                <a href="../index.html">← Back to Channels</a>
            </nav>
            <h1>#{channel}</h1>
            <div class="thread-controls">
                <a onclick="expandAllThreads()">Expand all {thread_count} threads</a>
                <a onclick="collapseAllThreads()">Collapse all {thread_count} threads</a>
            </div>
        """
        
        # Output messages with inline threads
        for msg in messages:
            # Skip thread replies - they'll be shown in their thread containers
            if msg.get('thread_ts') and msg['thread_ts'] != msg['ts']:
                continue
                
            html += self.format_message(msg)
            
            # If this message has replies, add the thread container
            thread_ts = msg['ts']
            if thread_ts in threads:
                thread_messages = sorted(threads[thread_ts], key=lambda x: float(x['ts']))
                reply_count = sum(1 for m in thread_messages if m['ts'] != thread_ts)
                if reply_count > 0:  # Only show thread UI if there are actual replies
                    html += f"""
                    <div>
                        <a class="thread-toggle" id="toggle-{thread_ts}" 
                           onclick="toggleThread('{thread_ts}')"
                           data-replies="{reply_count}">Show thread ({reply_count} replies) ↓</a>
                        <div class="thread-container" id="thread-{thread_ts}">
                    """
                    for thread_msg in thread_messages:
                        if thread_msg['ts'] != thread_ts:  # Skip parent message, already shown
                            html += self.format_message(thread_msg)
                    html += "</div></div>"
        
        html += """
        </body>
        </html>
        """
        return html

    def get_channel_user_stats(self, channel: str) -> List[tuple[str, int]]:
        """Get list of users and their message counts for a channel"""
        user_counts = {}  # user_id -> message count
        
        channel_path = self.get_data_path(f'export_data/{channel}')
        if not os.path.exists(channel_path):
            return []
        
        # Load users.json from the channel directory or parent directory
        users_file = os.path.join(os.path.dirname(channel_path), 'users.json')
        if not os.path.exists(users_file):
            users_file = os.path.join(channel_path, 'users.json')
        
        if os.path.exists(users_file):
            try:
                with open(users_file, 'r') as f:
                    users = json.load(f)
                    self.users_data = {u['id']: u for u in users}
            except Exception as e:
                log('error', f"Failed to load users file for channel stats: {e}")
        
        for filename in os.listdir(channel_path):
            if not filename.endswith('.json'):
                continue
            
            with open(os.path.join(channel_path, filename)) as f:
                try:
                    day_messages = json.load(f)
                    for msg in day_messages:
                        user_id = msg.get('user')
                        if user_id:
                            user_counts[user_id] = user_counts.get(user_id, 0) + 1
                except Exception as e:
                    log('error', f"Error processing {filename}: {e}")
                    continue
        
        # Convert to list of (username, count) tuples, sorted by count
        user_stats = [(self.get_username(uid), count) 
                      for uid, count in user_counts.items()]
        return sorted(user_stats, key=lambda x: x[1], reverse=True)

    def get_channel_activity_map(self, channel: str, days: int = 30) -> Dict[str, int]:
        """Get daily message counts for the last N days"""
        activity = {}
        channel_path = self.get_data_path(f'export_data/{channel}')
        
        if not os.path.exists(channel_path):
            return {}
        
        # Get all dates from json files and count messages by month
        monthly_counts = {}
        for filename in os.listdir(channel_path):
            if not filename.endswith('.json'):
                continue
            
            # Extract date from filename (assuming YYYY-MM-DD.json format)
            date = filename.replace('.json', '')
            if not date[0].isdigit():  # Skip non-date files
                continue
            
            try:
                date_obj = datetime.strptime(date, '%Y-%m-%d')
                month_key = date_obj.strftime('%Y-%m')
                
                with open(os.path.join(channel_path, filename)) as f:
                    messages = json.load(f)
                    monthly_counts[month_key] = monthly_counts.get(month_key, 0) + len(messages)
            except ValueError:
                continue
        
        if not monthly_counts:
            return {}
        
        # Get date range
        months = sorted(monthly_counts.keys())
        start_month = months[0]
        end_month = months[-1]
        
        return {
            'activity': monthly_counts,
            'start_date': datetime.strptime(start_month, '%Y-%m'),
            'end_date': datetime.strptime(end_month, '%Y-%m')
        }

    def get_export_info(self) -> Dict[str, str]:
        """Get workspace name and export date info"""
        info = {
            'workspace': 'Unknown Workspace',
            'workspace_url': None,
            'date_range': None  # Initialize as None to detect if we need to find it
        }
        
        # Try to get workspace URL from canvases.json if available
        canvases_file = self.get_data_path('export_data/canvases.json')
        if os.path.exists(canvases_file):
            try:
                with open(canvases_file) as f:
                    data = json.load(f)
                    if data and isinstance(data, list):
                        for canvas in data:
                            url = canvas.get('url', '')
                            if url and 'slack.com' in url:
                                # Extract workspace URL from canvas URL
                                # e.g., https://app.slack.com/canvas/TEAM123 -> https://TEAM123.slack.com
                                team_id = url.split('/')[-1]
                                info['workspace_url'] = f"https://{team_id}.slack.com"
                                break
            except:
                pass
        
        if self.zip_path:
            # Try to extract info from zip filename first
            zip_name = os.path.basename(self.zip_path)
            zip_name = zip_name.replace('.zip', '')
            
            if 'export' in zip_name.lower():
                parts = zip_name.split('export', 1)
                if parts:
                    info['workspace'] = parts[0].strip()
                    if len(parts) > 1:
                        info['date_range'] = parts[1].strip()
        
        # Try to get workspace name from channels.json if available
        channels_file = self.get_data_path('export_data/channels.json')
        if os.path.exists(channels_file):
            try:
                with open(channels_file) as f:
                    data = json.load(f)
                    if data and isinstance(data, list) and data[0].get('is_org_shared') is not None:
                        workspace = data[0].get('name', '').split('-')[0]
                        if workspace:
                            info['workspace'] = workspace
            except:
                pass
        
        # If date_range is still None, determine it from the channel data
        if not info['date_range']:
            earliest_date = None
            latest_date = None
            
            # Look through all channel directories
            export_dir = self.get_data_path('export_data')
            if os.path.exists(export_dir):
                for item in os.listdir(export_dir):
                    channel_path = os.path.join(export_dir, item)
                    if os.path.isdir(channel_path):
                        for filename in os.listdir(channel_path):
                            if filename.endswith('.json') and filename[0].isdigit():
                                try:
                                    date_str = filename.replace('.json', '')
                                    date = datetime.strptime(date_str, '%Y-%m-%d')
                                    if not earliest_date or date < earliest_date:
                                        earliest_date = date
                                    if not latest_date or date > latest_date:
                                        latest_date = date
                                except ValueError:
                                    continue
            
            if earliest_date and latest_date:
                if earliest_date.year == latest_date.year:
                    if earliest_date.month == latest_date.month:
                        info['date_range'] = f"{earliest_date.strftime('%B %Y')}"
                    else:
                        info['date_range'] = f"{earliest_date.strftime('%B')} - {latest_date.strftime('%B %Y')}"
                else:
                    info['date_range'] = f"{earliest_date.strftime('%B %Y')} - {latest_date.strftime('%B %Y')}"
        
        return info

    def generate_index_page(self, channels: List[str]) -> str:
        """Generate main index page with channel list"""
        export_info = self.get_export_info()
        
        # Get channel data for sorting
        channel_data = []
        
        # Find global start and end dates across all channels
        global_start = None
        global_end = None
        
        for channel in channels:
            stats = self.get_channel_stats(channel)
            activity_data = self.get_channel_activity_map(channel)
            
            if activity_data:
                start_date = activity_data.get('start_date')
                end_date = activity_data.get('end_date')
                if start_date:
                    if not global_start or start_date < global_start:
                        global_start = start_date
                if end_date:
                    if not global_end or end_date > global_end:
                        global_end = end_date
            
            # Calculate metrics for sorting
            recent_activity = None
            total_messages = stats['messages']
            
            if activity_data and activity_data.get('activity'):
                # Get most recent month with activity
                active_months = sorted(activity_data['activity'].keys())
                if active_months:
                    recent_activity = active_months[-1]
            
            # Calculate message count from stats instead of trying to parse activity text
            message_count = stats['messages']
            last_message = ""
            
            # Get most recent message timestamp
            if activity_data and activity_data.get('activity'):
                active_months = sorted(activity_data['activity'].keys())
                if active_months:
                    last_message = active_months[-1]  # Format is already YYYY-MM
            
            channel_data.append({
                'name': channel,
                'stats': stats,
                'recent_activity': recent_activity,
                'total_messages': total_messages,
                'user_stats': self.get_channel_user_stats(channel),
                'activity_data': activity_data,
                'message_count': message_count,
                'last_message': last_message
            })
        
        # Calculate activity graph width based on time interval
        months_width = 200  # Default minimum width
        if global_start and global_end:
            # Calculate number of months between start and end
            months_between = ((global_end.year - global_start.year) * 12 + 
                             global_end.month - global_start.month + 1)
            # Use 15px per month as minimum bar width
            calculated_width = max(200, months_between * 15)
            months_width = min(calculated_width, 400)  # Cap at 400px
        
        # Add debug logging
        log('debug', 'Generating index.html header')
        
        html = """<!DOCTYPE html>
<html>
<head>
    <title>Slack Export</title>
    <style>
        body { 
            font-family: Arial, sans-serif; 
            margin: 0 auto;
            padding: 20px;
            max-width: 1200px;
            background: #f5f5f5;
        }
        #channels-container {
            list-style: none;
            padding: 0;
        }
        .channel-item { 
            margin: 15px 0; 
            padding: 20px;
            border-radius: 8px;
            background: white;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            display: flex;  /* Make it a flex container */
            align-items: flex-start;  /* Align items to top */
            gap: 20px;     /* Space between content and graph */
        }
        .channel-name { 
            font-size: 1.2em;
            font-weight: bold;
            color: #1264A3;
            text-decoration: none;
        }
        .channel-name:hover {
            text-decoration: underline;
        }
        .channel-stats { 
            color: #666;
            margin-left: 10px;
            font-size: 0.9em;
        }
        .channel-content {
            flex: 1;       /* Take up remaining space */
            min-width: 0;  /* Allow content to shrink */
        }
        .channel-main {
            margin-bottom: 10px;
        }
        .activity-container {
            width: 300px;  /* Fixed width for the graph */
            flex-shrink: 0; /* Don't shrink the graph */
        }
        .activity-graph { 
            display: flex;
            align-items: flex-end;
            height: 40px;
            gap: 1px;
        }
        .activity-bar {
            flex: 1;
            background-color: #1264A3;
            opacity: 0.7;
            transition: height 0.2s ease;
        }
        .activity-bar:hover {
            opacity: 1;
        }
        .details {
            display: none;  /* Hidden by default */
            margin: 10px 0;
            padding: 15px;
            background: #f8f9fa;
            border-radius: 6px;
            font-size: 0.9em;
        }
        .details.show {
            display: block;  /* Show when .show class is added */
        }
        .details-toggle {
            color: #1264A3;
            cursor: pointer;
            font-size: 0.9em;
            text-decoration: none;
            padding: 4px 8px;
            border-radius: 4px;
            background: #f0f0f0;
            margin-left: 10px;
        }
        .details-toggle:hover {
            background: #e0e0e0;
        }
        .global-controls {
            margin: 10px 0 20px 0;
        }
        .global-controls a {
            color: #1264A3;
            text-decoration: none;
            margin-right: 20px;
            cursor: pointer;
        }
        .global-controls a:hover {
            text-decoration: underline;
        }
    </style>
</head>
<body>
    <h1>Slack Export</h1>
    
    <div class="global-controls">
        <a onclick="expandAllDetails()">Expand All Details</a>
        <a onclick="collapseAllDetails()">Collapse All Details</a>
    </div>
    
    <div class="sort-buttons">
        <button id="sort-alpha">Sort Alphabetically</button>
        <button id="sort-recent">Sort by Recently Active</button>
        <button id="sort-active">Sort by Most Active</button>
    </div>
    
    <div id="channels-container">"""

        log('debug', 'Generating channel entries')
        
        # Sort channels by recent activity by default
        channel_data.sort(
            key=lambda x: (
                x['recent_activity'] or '0000-00-00',  # Sort channels without activity to the end
                x['name']  # Secondary sort by name for channels with same activity date
            ),
            reverse=True  # Most recent first
        )
        
        def format_user_list(user_stats, available_width):
            """Format user list based on available width"""
            if not user_stats:
                return "No messages"
            
            # Adjust number of shown users based on width
            if available_width >= 1400:
                max_users = 6
            elif available_width >= 1200:
                max_users = 4
            elif available_width >= 900:
                max_users = 3
            else:
                max_users = 2
            
            user_display = []
            for username, count in user_stats[:max_users]:
                user_display.append(f"{username} ({count})")
            if len(user_stats) > max_users:
                remaining = len(user_stats) - max_users
                user_display.append(f"+{remaining} more")
            return ", ".join(user_display)
        
        for data in channel_data:
            channel = data['name']
            stats = data['stats']
            user_stats = data['user_stats']
            activity_data = data['activity_data']
            
            # Get channel summary from summary.txt if it exists
            summary = "No summary has been generated yet. Use -ai feature to fix this."
            summary_path = os.path.join(self.output_dir, channel, 'summary.txt')
            if os.path.exists(summary_path):
                try:
                    with open(summary_path, 'r', encoding='utf-8') as f:
                        summary = f.read().strip()
                except Exception as e:
                    log('error', f"Failed to read summary for {channel}: {e}")
            
            # Format user list with all users
            full_user_list = []
            for username, count in user_stats:
                full_user_list.append(f"{username} ({count})")
            user_list_html = "<br>".join(full_user_list) if full_user_list else "No messages"
            
            # Format user list with responsive width
            user_text = format_user_list(user_stats, 1200)  # Default to 1200px width
            
            # Generate activity graph
            activity_html = '<div class="activity-container">'
            activity_html += '<div class="activity-graph-wrapper">'
            activity_html += '<div class="activity-graph">'
            
            # Calculate number of months between global start and end
            if global_start and global_end:
                current = global_start
                while current <= global_end:
                    month_key = current.strftime('%Y-%m')
                    count = 0
                    if activity_data and activity_data['activity']:
                        count = activity_data['activity'].get(month_key, 0)
                        max_msgs = max((data['activity_data']['activity'].values() 
                                    for data in channel_data 
                                    if data['activity_data'] and data['activity_data']['activity']),
                                    key=lambda x: max(x) if x else 0)
                        max_msgs = max(max_msgs) if max_msgs else 0
                        height = int((count / max_msgs * 100) if max_msgs > 0 else 0)
                    else:
                        height = 0
                    
                    activity_html += f'<div class="activity-bar" style="height: {height}%" title="{month_key}: {count} messages"></div>'
                    current = (current.replace(day=1) + timedelta(days=32)).replace(day=1)
            
            activity_html += '</div></div></div>'
            
            # Add data attributes for sorting
            recent_activity_attr = f' data-recent-activity="{data["recent_activity"]}"' if data["recent_activity"] else ''
            message_count_attr = f' data-message-count="{data["total_messages"]}"'
            
            html += f"""
            <li class="channel-item" 
                data-name="{channel}"
                data-recent-activity="{data['recent_activity'] or ''}"
                data-message-count="{data['message_count']}">
                <div class="channel-content">
                    <div class="channel-main">
                        <a href="{channel}/index.html" class="channel-name">#{channel}</a>
                        <span class="channel-stats">
                            {stats['messages']} messages • 
                            {stats['threads']} threads • 
                            {stats['attachments']} files
                            {f" • {stats['date_range']}" if stats['date_range'] else ""}
                        </span>
                        <a class="details-toggle" id="toggle-details-{channel}" 
                           onclick="toggleDetails('{channel}')">Show Details ↓</a>
                    </div>
                    <div class="details" id="details-{channel}">
                        <strong>Users:</strong><br>
                        {user_list_html}<br><br>
                        <strong>Summary:</strong><br>
                        {summary}
                    </div>
                    <span class="user-list">{user_text}</span>
                </div>
                {activity_html}
            </li>
            """
        
        html += """
        </div>
        <script>
            function expandAllDetails() {
                document.querySelectorAll('.details').forEach(details => {
                    details.classList.add('show');
                    const toggle = document.getElementById('toggle-details-' + details.id.replace('details-', ''));
                    if (toggle) toggle.textContent = 'Hide Details ↑';
                });
            }
            
            function collapseAllDetails() {
                document.querySelectorAll('.details').forEach(details => {
                    details.classList.remove('show');
                    const toggle = document.getElementById('toggle-details-' + details.id.replace('details-', ''));
                    if (toggle) toggle.textContent = 'Show Details ↓';
                });
            }

            function toggleDetails(channelId) {
                const details = document.getElementById('details-' + channelId);
                const toggle = document.getElementById('toggle-details-' + channelId);
                if (details.classList.contains('show')) {
                    details.classList.remove('show');
                    toggle.textContent = 'Show Details ↓';
                } else {
                    details.classList.add('show');
                    toggle.textContent = 'Hide Details ↑';
                }
                return false;  // Prevent default link behavior
            }

            document.addEventListener('DOMContentLoaded', function() {
                function sortChannels(method) {
                    const container = document.getElementById('channels-container');
                    const channels = Array.from(container.getElementsByClassName('channel-item'));
                    
                    channels.sort((a, b) => {
                        if (method === 'alpha') {
                            return a.getAttribute('data-name').localeCompare(b.getAttribute('data-name'));
                        } else if (method === 'recent') {
                            const aDate = a.getAttribute('data-recent-activity') || '';
                            const bDate = b.getAttribute('data-recent-activity') || '';
                            return bDate.localeCompare(aDate);
                        } else if (method === 'active') {
                            const aCount = parseInt(a.getAttribute('data-message-count') || '0');
                            const bCount = parseInt(b.getAttribute('data-message-count') || '0');
                            return bCount - aCount;
                        }
                    });
                    
                    channels.forEach(channel => {
                        container.removeChild(channel);
                        container.appendChild(channel);
                    });
                }

                document.getElementById('sort-alpha').addEventListener('click', () => sortChannels('alpha'));
                document.getElementById('sort-recent').addEventListener('click', () => sortChannels('recent'));
                document.getElementById('sort-active').addEventListener('click', () => sortChannels('active'));
            });
        </script>
    </body>
</html>"""
        
        log('debug', 'Index page generation complete')
        return html

    def get_channel_stats(self, channel: str) -> Dict[str, int]:
        """Get statistics for a channel"""
        messages = 0
        attachments = 0
        threads = set()
        earliest_date = None
        latest_date = None
        
        # Get channel path from zip file using get_data_path
        channel_path = self.get_data_path(f'export_data/{channel}')
        
        if not os.path.exists(channel_path):
            log('warning', f"No message data found for {channel} in zip file")
            return {
                'messages': 0,
                'attachments': 0,
                'threads': 0,
                'date_range': None
            }
        
        for filename in os.listdir(channel_path):
            if not filename.endswith('.json') or not filename[0].isdigit():
                continue
            
            # Track date range from filenames
            try:
                date_str = filename.replace('.json', '')
                date = datetime.strptime(date_str, '%Y-%m-%d')
                if not earliest_date or date < earliest_date:
                    earliest_date = date
                if not latest_date or date > latest_date:
                    latest_date = date
            except ValueError:
                continue
            
            with open(os.path.join(channel_path, filename)) as f:
                day_messages = json.load(f)
                messages += len(day_messages)
                
                for msg in day_messages:
                    if 'files' in msg:
                        attachments += len(msg['files'])
                    if 'thread_ts' in msg:
                        threads.add(msg['thread_ts'])
        
        # Format date range string
        date_range = None
        if earliest_date and latest_date:
            if earliest_date.year == latest_date.year:
                if earliest_date.month == latest_date.month:
                    date_range = earliest_date.strftime('%B %Y')
                else:
                    date_range = f"{earliest_date.strftime('%B')} - {latest_date.strftime('%B %Y')}"
            else:
                date_range = f"{earliest_date.strftime('%B %Y')} - {latest_date.strftime('%B %Y')}"
        
        return {
            'messages': messages,
            'attachments': attachments,
            'threads': len(threads),
            'date_range': date_range
        }

    def process_channel(self, channel: str) -> None:
        """Process a channel's messages and generate HTML and text transcript"""
        channel_dir = os.path.join(self.output_dir, channel)
        os.makedirs(channel_dir, exist_ok=True)
        
        all_messages = []
        threads = {}  # Store threads by thread_ts
        messages_with_replies = set()  # Track which messages have replies
        thread_reply_counts = {}  # Track number of replies per thread
        
        # Get the channel directory path
        channel_data_path = self.get_data_path(f'export_data/{channel}')
        if not os.path.exists(channel_data_path):
            log('warning', f"Channel directory not found: {channel_data_path}")
            return
        
        # First pass: collect all messages and identify threads
        for filename in sorted(os.listdir(channel_data_path)):
            if not filename.endswith('.json'):
                continue
                
            with open(os.path.join(channel_data_path, filename)) as f:
                messages = json.load(f)
                for msg in messages:
                    # Add default timestamp for sorting
                    if 'ts' not in msg:
                        log('warning', f"Message without timestamp in {channel}/{filename}, id: {msg.get('id', 'unknown')}")
                        msg['ts'] = '0'  # Will sort to the beginning
                        
                    processed_msg = self.process_message(msg, channel)
                    all_messages.append(processed_msg)
                    
                    # Track thread messages
                    thread_ts = msg.get('thread_ts')
                    if thread_ts:
                        log('debug', f"Found message in thread: ts={msg['ts']}, thread_ts={thread_ts}")
                        
                        # Count replies for this thread
                        if thread_ts not in thread_reply_counts:
                            thread_reply_counts[thread_ts] = 0
                        
                        # If this is a reply (not the parent)
                        if thread_ts != msg['ts']:
                            thread_reply_counts[thread_ts] += 1
                            messages_with_replies.add(thread_ts)  # Mark the parent message
                            log('debug', f"Marked message {thread_ts} as having replies (count: {thread_reply_counts[thread_ts]})")
                        
                        # Collect all thread messages
                        if thread_ts not in threads:
                            threads[thread_ts] = []
                        threads[thread_ts].append(processed_msg)
                        log('debug', f"Added message to thread {thread_ts}, total messages: {len(threads[thread_ts])}")

        if not all_messages:
            log('warning', f"No valid messages found in channel {channel}")
            return

        # Sort all messages by timestamp
        all_messages.sort(key=lambda x: float(x['ts']))

        # Mark messages that have replies
        for msg in all_messages:
            thread_ts = msg['ts']
            if thread_ts in messages_with_replies:
                msg['has_replies'] = True
                reply_count = thread_reply_counts.get(thread_ts, 0)
                log('debug', f"Message {thread_ts} has {reply_count} replies")
            else:
                msg['has_replies'] = False

        # Generate main channel page (HTML)
        channel_html = self.generate_channel_page(channel, all_messages)
        with open(os.path.join(channel_dir, 'index.html'), 'w', encoding='utf-8') as f:
            f.write(channel_html)

        # Generate text transcript
        channel_text = self.generate_channel_transcript(channel, all_messages, threads)
        with open(os.path.join(channel_dir, 'index.txt'), 'w', encoding='utf-8') as f:
            f.write(channel_text)

        # After processing all messages, write file reports
        self.write_file_reports(channel)

    def generate_channel_transcript(self, channel: str, messages: List[Dict], threads: Dict) -> str:
        """Generate a text-only transcript of the channel"""
        transcript = f"Channel: #{channel}\n\n"
        
        current_date = None
        
        for msg in messages:
            # Skip thread replies - they'll be shown under their parent message
            if msg.get('thread_ts') and msg['thread_ts'] != msg['ts']:
                continue

            # Check if we need to print a new date header
            msg_date = datetime.fromtimestamp(float(msg['ts'])).strftime('%Y-%m-%d')
            if msg_date != current_date:
                current_date = msg_date
                # Format date more nicely for display
                display_date = datetime.fromtimestamp(float(msg['ts'])).strftime('%B %d, %Y')
                transcript += f"\n=== {display_date} ===\n\n"

            username = self.get_username(msg.get('user', ''))

            # Format message text
            text = msg.get('text', '')
            # Replace user mentions with @username
            if '<@' in text:
                for user_id in self.users_data:
                    text = text.replace(f'<@{user_id}>', f'@{self.get_username(user_id)}')

            # Basic message
            transcript += f"{username}:\n    {text}\n"

            # Handle files
            if 'files' in msg:
                for file_info in msg['files']:
                    name = file_info.get('name', file_info.get('id', 'Unknown file'))
                    local_path = file_info.get('local_path')
                    if file_info.get('download_failed'):
                        reason = file_info.get('failure_reason', 'unknown reason')
                        transcript += f"    [File: {name} ({reason})]\n"
                    elif local_path:
                        transcript += f"    [File: {name} -> {local_path}]\n"
                    else:
                        transcript += f"    [File: {name} (no local path)]\n"

            # Handle thread replies
            thread_ts = msg['ts']
            if thread_ts in threads:
                thread_messages = sorted(threads[thread_ts], key=lambda x: float(x['ts']))
                # Skip the parent message (already shown)
                thread_messages = [m for m in thread_messages if m['ts'] != thread_ts]
                if thread_messages:
                    transcript += "    Thread replies:\n"
                    for reply in thread_messages:
                        reply_user = self.get_username(reply.get('user', ''))
                        reply_text = reply.get('text', '')
                        # Replace user mentions in replies
                        if '<@' in reply_text:
                            for user_id in self.users_data:
                                reply_text = reply_text.replace(f'<@{user_id}>', f'@{self.get_username(user_id)}')
                        transcript += f"        {reply_user}:\n            {reply_text}\n"
                        
                        # Handle files in replies
                        if 'files' in reply:
                            for file_info in reply['files']:
                                name = file_info.get('name', 'Unknown file')
                                local_path = file_info.get('local_path')
                                if local_path:
                                    transcript += f"            [File: {name} -> {local_path}]\n"
                                else:
                                    transcript += f"            [File: {name} (download failed)]\n"
                    transcript += "\n"

            transcript += "\n"

        return transcript

    def generate_thread_page(self, channel: str, messages: List[Dict]) -> str:
        """Generate HTML for a thread"""
        # Mark all messages as being in thread view
        for msg in messages:
            msg['_in_thread_view'] = True
            
        parent_msg = messages[0]  # First message is the parent
        parent_time = datetime.fromtimestamp(float(parent_msg['ts'])).strftime('%Y-%m-%d %H:%M:%S')
        parent_user = self.get_username(parent_msg.get('user', ''))
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Thread in #{channel}</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                .message {{ margin: 10px 0; padding: 10px; border-bottom: 1px solid #eee; }}
                .timestamp {{ color: #666; font-size: 0.8em; }}
                .user {{ font-weight: bold; color: #1264A3; }}
                .attachment {{ margin: 10px 0; }}
                .attachment img {{ max-width: 400px; }}
                .failed-download {{ 
                    color: #666;
                    font-style: italic;
                }}
                nav {{ margin-bottom: 20px; }}
                .thumbnail {{
                    max-width: 200px;
                    max-height: 200px;
                    object-fit: contain;
                    cursor: pointer;
                }}
                .thumbnail-link {{
                    display: inline-block;
                    text-decoration: none;
                }}
                .thread-header {{
                    margin-bottom: 30px;
                }}
                .thread-info {{
                    color: #666;
                    font-size: 0.9em;
                    margin-top: 5px;
                }}
            </style>
        </head>
        <body>
            <nav>
                <a href="index.html">← Back to #{channel}</a>
            </nav>
            <div class="thread-header">
                <h2>Thread in #{channel}</h2>
                <div class="thread-info">Started by {parent_user} on {parent_time}</div>
            </div>
        """
        
        # Generate messages HTML
        for msg in messages:
            html += self.format_message(msg)
        
        html += """
        </body>
        </html>
        """
        return html

    def format_message(self, msg: Dict) -> str:
        """Format a single message for display"""
        # Handle timestamp display
        if 'ts' not in msg or msg['ts'] == '0':
            timestamp_display = "[No Timestamp]"
        else:
            timestamp_display = datetime.fromtimestamp(float(msg['ts'])).strftime('%Y-%m-%d %H:%M:%S')
        
        # Handle text with user mentions and system messages
        text = msg.get("text", "")
        
        # First handle Slack's special formatting
        # Handle user mentions before escaping
        if '<@' in text:
            for user_id in self.users_data:
                text = text.replace(f'<@{user_id}>', f'@{self.get_username(user_id)}')
        
        # Now escape HTML after processing Slack formatting
        text = html.escape(text)
        
        username = self.get_username(msg.get('user', ''))
        
        message_html = f"""
        <div class="message">
            <div class="timestamp">{timestamp_display}</div>
            <div class="user">{username}</div>
        """
        
        # Handle blocks if they exist
        if 'blocks' in msg:
            message_html += f'<div class="text">{self.process_blocks(msg["blocks"])}</div>'
        else:
            message_html += f'<div class="text">{text}</div>'
        
        # Handle files
        if 'files' in msg:
            for file_info in msg['files']:
                message_html += '<div class="file">'
                if file_info.get('download_failed'):
                    message_html += f'<div class="failed-download">File download failed: {file_info.get("name", "Unknown file")}</div>'
                else:
                    local_path = file_info.get('local_path')
                    if local_path:
                        name = file_info.get('name', 'Unknown file')
                        # Check file extension for images
                        image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}
                        is_image = any(local_path.lower().endswith(ext) for ext in image_extensions)
                        
                        if is_image:
                            message_html += f"""
                            <div class="image-container">
                                <img src="{local_path}" alt="{name}" class="message-image">
                                <div class="image-caption">
                                    <a href="{local_path}" target="_blank">{name}</a>
                                </div>
                            </div>
                            """
                        else:
                            # For non-images, just show the filename as a link
                            message_html += f'<div class="file-link"><a href="{local_path}" target="_blank">{name}</a></div>'
                message_html += '</div>'
        
        message_html += '</div>'
        return message_html

    def write_file_reports(self, channel: str) -> None:
        """Write reports of missing and downloaded files for a channel in CSV format"""
        channel_dir = os.path.join(self.output_dir, channel)
        
        # Write missing files report
        missing_files = getattr(self, 'channel_missing_files', {}).get(channel, [])
        if missing_files:
            report_path = os.path.join(channel_dir, 'files_missing.csv')
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write('timestamp,file_id,mode\n')
                for file in sorted(missing_files, key=lambda x: float(x['timestamp'])):
                    timestamp = datetime.fromtimestamp(float(file['timestamp'])).strftime('%Y-%m-%d %H:%M:%S')
                    f.write(f"{timestamp},{file['file_id']},{file['mode']}\n")
            log('info', 'Found {count} missing files in channel {channel}', 
                channel=channel, count=len(missing_files))
        else:
            log('info', 'No missing files found in channel {channel}', channel=channel)
        
        # Write downloaded files report
        downloaded_files = getattr(self, 'channel_downloaded_files', {}).get(channel, [])
        if downloaded_files:
            report_path = os.path.join(channel_dir, 'files_downloaded.csv')
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write('timestamp,file_id,mode\n')
                for file in sorted(downloaded_files, key=lambda x: float(x['timestamp'])):
                    timestamp = datetime.fromtimestamp(float(file['timestamp'])).strftime('%Y-%m-%d %H:%M:%S')
                    f.write(f"{timestamp},{file['file_id']},{file['mode']}\n")
            log('info', 'Found {count} available files in channel {channel}', 
                channel=channel, count=len(downloaded_files))
        else:
            log('info', 'No files were downloaded in channel {channel}', channel=channel)

def main():
    setup_logging()  # Initialize logging for main execution
    parser = argparse.ArgumentParser(
        description='Generate static website from Slack export data',
        epilog="""
Examples:
  Process all channels in export:
    %(prog)s slack_export.zip

  Process specific channels:
    %(prog)s slack_export.zip -channels channel1 channel2 channel3
    
  Process existing channel directories:
    %(prog)s slack_export.zip -channels-existing
    
  Force rewrite all files:
    %(prog)s slack_export.zip -force-rewrite
""",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('zip_file', help='Slack export zip file to process')
    channel_group = parser.add_mutually_exclusive_group()
    channel_group.add_argument('-channels', nargs='+', help='Specific channels to process')
    channel_group.add_argument('-channels-existing', action='store_true', 
                             help='Process existing channel directories in output path')
    parser.add_argument('-o', '--output', default='output', 
                       help='Output directory path (default: output)')
    parser.add_argument('-force-rewrite', action='store_true',
                       help='Force rewrite all files even if they exist')
    args = parser.parse_args()
    
    # Validate zip file exists
    if not os.path.exists(args.zip_file):
        log('error', 'Zip file not found: {file}', file=args.zip_file)
        sys.exit(1)

    # Create viewer with zip file and specified output directory
    viewer = SlackExportViewer(output_dir=args.output, zip_path=args.zip_file)
    
    # Load channel and user data
    channels_file = viewer.get_data_path('export_data/channels.json')
    users_file = viewer.get_data_path('export_data/users.json')
    
    if not os.path.exists(channels_file):
        log('error', 'channels.json not found in zip file')
        sys.exit(1)
    if not os.path.exists(users_file):
        log('error', 'users.json not found in zip file')
        sys.exit(1)
        
    viewer.load_channels(channels_file)
    viewer.load_users(users_file)
    
    # Determine channels to process
    if args.channels_existing:
        # Use existing directories in output path
        if not os.path.exists(args.output):
            log('error', 'Output directory not found: {dir}', dir=args.output)
            sys.exit(1)
        channels_to_process = [d for d in os.listdir(args.output) 
                             if os.path.isdir(os.path.join(args.output, d))]
        if not channels_to_process:
            log('error', 'No channel directories found in: {dir}', dir=args.output)
            sys.exit(1)
    else:
        channels_to_process = args.channels if args.channels else list(viewer.channels_data.keys())
    
    # Validate specified channels exist in the export
    if args.channels:
        for channel in channels_to_process:
            if channel not in viewer.channels_data:
                log('error', 'Channel not found: {channel}', channel=channel)
                sys.exit(1)
    
    # Create output directory
    os.makedirs(args.output, exist_ok=True)
    
    # Keep track of processed channels
    processed_channels = []
    
    # Process each channel
    for channel in channels_to_process:
        channel_dir = os.path.join(args.output, channel)
        html_path = os.path.join(channel_dir, 'index.html')
        txt_path = os.path.join(channel_dir, 'index.txt')
        
        # Only process if files don't exist or force rewrite is enabled
        if args.force_rewrite or not os.path.exists(html_path) or not os.path.exists(txt_path):
            missing = []
            if args.force_rewrite:
                missing = ['index.html', 'index.txt']
                log('info', 'Processing channel {channel} - force rewriting {files} and downloading referenced files', 
                    channel=channel, files=', '.join(missing))
            else:
                if not os.path.exists(html_path):
                    missing.append('index.html')
                if not os.path.exists(txt_path):
                    missing.append('index.txt')
                log('info', 'Processing channel {channel} - generating {files} and downloading referenced files', 
                    channel=channel, files=', '.join(missing))
            viewer.process_channel(channel)
        else:
            log('info', 'Processing channel {channel} - downloading referenced files', channel=channel)
            viewer.process_channel(channel)
        
        processed_channels.append(channel)
        
        # Always update index page if force rewrite is enabled
        if args.force_rewrite or len(processed_channels) == len(channels_to_process):
            log('debug', 'Updating index.html with {count} channels', count=len(processed_channels))
            html = viewer.generate_index_page(processed_channels)
            with open(os.path.join(args.output, 'index.html'), 'w', encoding='utf-8') as f:
                f.write(html)
    
    log('info', 'Done! Open {path}/index.html in your browser to view the export.', 
        path=args.output)

if __name__ == '__main__':
    setup_logging()  # Initialize logging for main execution
    main() 