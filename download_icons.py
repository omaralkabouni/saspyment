import os
import urllib.request

icons_dir = r"f:\\Downloads\\zawdni appp\\app sas\\static\\icons"
os.makedirs(icons_dir, exist_ok=True)

try:
    urllib.request.urlretrieve('https://via.placeholder.com/192/1e293b/FFFFFF?text=SAS', os.path.join(icons_dir, 'icon-192x192.png'))
    urllib.request.urlretrieve('https://via.placeholder.com/512/1e293b/FFFFFF?text=SAS', os.path.join(icons_dir, 'icon-512x512.png'))
    print('Icons downloaded successfully.')
except Exception as e:
    print('Error downloading icons:', e)
