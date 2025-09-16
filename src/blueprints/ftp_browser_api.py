"""
FTP Browser Blueprint for InkyPi

This provides API endpoints for the FTP Browser plugin to:
- List directories
- Get image previews
"""

import os
import base64
import tempfile
import logging
from ftplib import FTP, FTP_TLS, error_perm
from PIL import Image
from flask import Blueprint, request, jsonify, current_app
from io import BytesIO

logger = logging.getLogger(__name__)

# Create blueprint
ftp_browser_api = Blueprint("ftp_browser_api", __name__)

def _connect_ftp(server, username="anonymous", password="", use_tls=False, passive=True):
    """Establish FTP/FTPS connection and login."""
    try:
        ftp_class = FTP_TLS if use_tls else FTP
        ftp = ftp_class()
        ftp.connect(server)
        ftp.login(user=username, passwd=password)
        ftp.set_pasv(passive)
        return ftp
    except Exception as e:
        logger.error(f"FTP connection error: {e}")
        raise RuntimeError(f"Failed to connect to FTP server: {e}")

def _list_directory(ftp, path="/"):
    """
    List contents of the specified directory.
    Returns dictionary with directories and files separated.
    """
    try:
        ftp.cwd(path)
        
        # Try to use MLSD command for more detailed listing
        try:
            entries = list(ftp.mlsd())
            
            # Separate directories and files
            directories = []
            files = []
            image_exts = (".jpg", ".jpeg", ".png", ".bmp", ".gif")
            
            for name, facts in entries:
                if name in (".", ".."):
                    continue
                    
                entry_type = facts.get("type", "")
                if entry_type == "dir":
                    directories.append({
                        "name": name,
                        "path": os.path.join(path, name).replace("\\", "/"),
                        "type": "dir"
                    })
                elif entry_type == "file" and name.lower().endswith(image_exts):
                    files.append({
                        "name": name,
                        "path": os.path.join(path, name).replace("\\", "/"),
                        "type": "file",
                        "size": facts.get("size", "0")
                    })
            
        except (AttributeError, error_perm):
            # Fallback to NLST if MLSD is not available
            names = ftp.nlst()
            
            # We don't have file type info with NLST, so we'll make an educated guess
            directories = []
            files = []
            image_exts = (".jpg", ".jpeg", ".png", ".bmp", ".gif")
            
            for name in names:
                if name in (".", ".."):
                    continue
                    
                full_path = os.path.join(path, name).replace("\\", "/")
                
                # Try to determine if it's a directory by attempting to CWD to it
                try:
                    curr_dir = ftp.pwd()
                    ftp.cwd(full_path)
                    ftp.cwd(curr_dir)  # Go back to original directory
                    
                    directories.append({
                        "name": name,
                        "path": full_path,
                        "type": "dir"
                    })
                except error_perm:
                    # If we can't CWD to it, it's probably a file
                    if name.lower().endswith(image_exts):
                        files.append({
                            "name": name,
                            "path": full_path,
                            "type": "file",
                            "size": "unknown"
                        })
        
        # Sort directories and files alphabetically
        directories.sort(key=lambda x: x["name"].lower())
        files.sort(key=lambda x: x["name"].lower())
        
        return {
            "current_path": path,
            "parent_path": os.path.dirname(path) if path != "/" else "/",
            "directories": directories,
            "files": files
        }
        
    except Exception as e:
        logger.error(f"Error listing directory {path}: {e}")
        raise RuntimeError(f"Failed to list directory {path}: {e}")

def _download_image(ftp, remote_path):
    """Download remote_path to a temporary local file; return local filepath."""
    try:
        suffix = os.path.splitext(remote_path)[1] or ".png"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
            local_path = tmp_file.name

            def callback(data):
                tmp_file.write(data)

            ftp.retrbinary(f"RETR {remote_path}", callback)

        return local_path
    except Exception as e:
        logger.error(f"Error downloading image {remote_path}: {e}")
        raise RuntimeError(f"Failed to download image {remote_path}: {e}")

@ftp_browser_api.route("/list_directory", methods=["POST"])
def list_directory():
    """API endpoint to list contents of a directory on an FTP server."""
    try:
        data = request.json
        
        server = data.get("server")
        username = data.get("username", "anonymous")
        password = data.get("password", "")
        use_tls = data.get("use_tls", False)
        passive = data.get("passive", True)
        path = data.get("path", "/")
        
        if not server:
            return jsonify({"error": "Server is required"}), 400
            
        ftp = _connect_ftp(server, username, password, use_tls, passive)
        try:
            result = _list_directory(ftp, path)
            return jsonify(result)
        finally:
            try:
                ftp.quit()
            except Exception:
                pass
    
    except Exception as e:
        logger.error(f"Error in list_directory endpoint: {e}")
        return jsonify({"error": str(e)}), 500

@ftp_browser_api.route("/preview_image", methods=["POST"])
def preview_image():
    """API endpoint to get a preview of an image from an FTP server."""
    try:
        data = request.json
        
        server = data.get("server")
        username = data.get("username", "anonymous")
        password = data.get("password", "")
        use_tls = data.get("use_tls", False)
        passive = data.get("passive", True)
        path = data.get("path")
        
        if not server:
            return jsonify({"error": "Server is required"}), 400
        if not path:
            return jsonify({"error": "Image path is required"}), 400
            
        ftp = _connect_ftp(server, username, password, use_tls, passive)
        local_path = None
        
        try:
            local_path = _download_image(ftp, path)
            
            # Create a thumbnail
            img = Image.open(local_path).convert("RGB")
            img.thumbnail((200, 200))
            
            # Convert to base64
            buffer = BytesIO()
            img.save(buffer, format="JPEG")
            img_str = base64.b64encode(buffer.getvalue()).decode("utf-8")
            
            return jsonify({"preview": img_str})
            
        finally:
            try:
                ftp.quit()
            except Exception:
                pass
                
            if local_path and os.path.exists(local_path):
                os.remove(local_path)
    
    except Exception as e:
        logger.error(f"Error in preview_image endpoint: {e}")
        return jsonify({"error": str(e)}), 500