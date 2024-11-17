import logging
import tempfile
import os

import dotenv
import ffmpeg
import requests

from io import BytesIO
from logging.handlers import RotatingFileHandler

from atproto import Client, IdResolver
from flask import Flask, jsonify, render_template, request, send_file, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix


# ----- Functions ----- #

def parse_bsky_url(url):
    # Handle bsky.app format: https://bsky.app/profile/steviesaid.bsky.social/post/3laxj7gxgwk2e
    if 'bsky.app/profile/' in url:
        parts = url.split('/profile/')[1].split('/')
        handle = parts[0]  # gets 'steviesaid.bsky.social'
        post_id = parts[2] if len(parts) > 2 else None  # gets '3laxj7gxgwk2e'
        return handle, post_id
    
    # Keep existing parsing for direct domain format as fallback
    # https://steviesaid.bsky.social/post/3laxj7gxgwk2e
    parts = url.replace('https://', '').split('/')
    handle = parts[0]
    post_id = parts[2] if len(parts) > 2 else None
    return handle, post_id

def download_video(segments: list) -> BytesIO:
    # Create a temporary directory for segments
    temp_dir = tempfile.mkdtemp()
    temp_segments = []
    output_path = os.path.join(temp_dir, 'output.mp4')
    
    try:
        # Download each segment
        for i, segment_url in enumerate(segments):
            segment_path = os.path.join(temp_dir, f'segment_{i}.ts')
            response = requests.get(segment_url)
            with open(segment_path, 'wb') as f:
                f.write(response.content)
            temp_segments.append(segment_path)
        
        # Create segments list file
        segments_file = os.path.join(temp_dir, 'segments.txt')
        with open(segments_file, 'w') as f:
            for segment_path in temp_segments:
                f.write(f"file '{segment_path}'\n")
        
        # Use ffmpeg to concatenate segments
        ffmpeg.input(segments_file, format='concat', safe=0) \
              .output(output_path, c='copy') \
              .overwrite_output() \
              .run(capture_stdout=True, capture_stderr=True)
        
        # Read the processed video into memory
        with open(output_path, 'rb') as f:
            video_buffer = BytesIO(f.read())
        
        return video_buffer
    
    finally:
        # Clean up temporary files
        for file in os.listdir(temp_dir):
            os.unlink(os.path.join(temp_dir, file))
        os.rmdir(temp_dir)

def setup_logging(app):
    """Configure logging for production"""
    if not os.path.exists('logs'):
        os.mkdir('logs')
    
    # Clear existing handlers
    app.logger.handlers.clear()
    
    # Configure logging to stdout for fly.io
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
    ))
    stream_handler.setLevel(logging.INFO)
    app.logger.addHandler(stream_handler)
    
    # Also keep a local log file
    file_handler = RotatingFileHandler(
        'logs/app.log',
        maxBytes=1024 * 1024,  # 1MB
        backupCount=10
    )
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
    ))
    file_handler.setLevel(logging.INFO)
    app.logger.addHandler(file_handler)
    
    app.logger.setLevel(logging.INFO)
    app.logger.info('Application startup')


# ----- Initialization ----- #

app = Flask(__name__)

# Configure app
app.config['MAX_CONTENT_LENGTH'] = 256 * 1024 * 1024  # 256MB max file size
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Setup logging for production
if not app.debug:
    setup_logging(app)

client = Client()
resolver = IdResolver()

# Load environment variables
dotenv.load_dotenv()
try:
    log_in = client.login(os.environ['bsky_user'], os.environ['bsky_pass'])
    app.logger.info('Successfully logged in to Bluesky')
except Exception as e:
    app.logger.error(f'Failed to log in to Bluesky: {e}')
    raise


# ----- Error Handlers —— #

@app.errorhandler(404)
def not_found_error(error):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    app.logger.error(f'Server Error: {error}')
    return jsonify({'error': 'Internal server error'}), 500


# ----- Routes ----- #

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'),
                               'bsvdl.ico', mimetype='image/vnd.microsoft.icon')

@app.route('/health')
def health():
    try:
        # Check if we can still make API calls
        resolver.handle.resolve('bsky.app')
        return {'status': 'healthy', 'bluesky': 'connected'}, 200
    except Exception as e:
        app.logger.error(f'Health check failed: {e}')
        return {'status': 'unhealthy', 'error': str(e)}, 500

@app.route('/process', methods=['POST'])
def process():
    post_url = request.form.get('post_url')
    quality = request.form.get('quality', '320p')

    if not post_url:
        return jsonify({
            'status': 'error',
            'message': 'No post URL provided'
        }), 400

    app.logger.info(f'Processing request for URL: {post_url} with quality: {quality}')

    try:
        # Parse Bluesky post URL
        handle, post_id = parse_bsky_url(post_url)
        app.logger.debug(f'Parsed handle: {handle}, post_id: {post_id}')

        # Get post data
        author_did = resolver.handle.resolve(handle)
        post_uri = f'at://{author_did}/app.bsky.feed.post/{post_id}'
        post = client.get_post_thread(post_uri).thread.post

        if 'recordWithMedia' in post.embed.py_type:
            # For reposts
            video_ref_link = post.record.embed.media.video.ref.link
        elif 'video#view' in post.embed.py_type:
            # For regular posts or replies
            video_ref_link = post.record.embed.video.ref.link
        else:
            raise ValueError(f'Unsupported type: {post.py_type}. Be sure your link is a post with a video in it.')

        # Build video URLs
        video_base_url = f'https://video.bsky.app/watch/{author_did}/{video_ref_link}'
        video_playlist_url = f'{video_base_url}/playlist.m3u8'
        video_playlist_m3u8 = requests.get(video_playlist_url).text

        # Get segments based on quality
        if quality == '320p':
            m3u8_uri = [ x for x in video_playlist_m3u8.split('\n') if x.startswith('360p') ][0]
            video_m3u8 = requests.get(f'{video_base_url}/{m3u8_uri}').text
            segments = [ f'{video_base_url}/360p/{x}' for x in video_m3u8.split('\n') if x.startswith('video') ]
            filename = f'{post_id}_360p.mp4'
        else:
            m3u8_uri = [ x for x in video_playlist_m3u8.split('\n') if x.startswith('720p') ][0]
            video_m3u8 = requests.get(f'{video_base_url}/{m3u8_uri}').text
            segments = [ f'{video_base_url}/720p/{x}' for x in video_m3u8.split('\n') if x.startswith('video') ]
            filename = f'{post_id}_720p.mp4'

        # Generate video buffer
        video_buffer = download_video(segments)

        # Send file directly
        app.logger.info(f'Successfully processed video for post: {post_id}')
        return send_file(
            video_buffer,
            mimetype='video/mp4',
            as_attachment=True,
            download_name=filename
        )

    except Exception as e:
        app.logger.error(f'Error processing {post_url}: {str(e)}', exc_info=True)
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 400


if __name__ == '__main__':
    app.run(debug=True)
