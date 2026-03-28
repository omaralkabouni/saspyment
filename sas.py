import requests
import time
import aes
import json
import urllib3

# Suppress insecure request warnings due to self-signed SSL on IP
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HEADERS = {
    'content-type':'application/json',
}

class SasAPI():
    def __init__(self, url, portal='admin'):
        self.root_url = url.rstrip('/')
        self.portal = portal
        self.base_url = f"{self.root_url}/{self.portal}/api/index.php/api/"
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.session.verify = False
        self.attempts = [] # Track URL discovery attempts

    def _get_url_variants(self):
        """Common SAS Radius API path patterns."""
        p = self.portal
        u = self.root_url
        return [
            f"{u}/{p}/index.php/api/",       # Pattern that gave 200 in logs
            f"{u}/{p}/api/index.php/api/",   # Original
            f"{u}/{p}/api/index.php/",
            f"{u}/index.php/api/",
            f"{u}/{p}/api/",
            f"{u}/api/v1/index.php/api/",
            f"{u}/api/v1/{p}/index.php/api/"
        ]

    def login(self, username, password):
        self.attempts = [] # Clear previous attempts
        variants = self._get_url_variants()
        payload = aes.encrypt(json.dumps({
            'username': username,
            'password': password
        }))
        data = {'payload': payload}
        
        last_error = "Unknown error"
        
        for variant in variants:
            login_url = variant + "login"
            try:
                response = self.session.post(login_url, json=data, timeout=8)
                resp_text = response.text[:100].replace('\n', ' ')
                
                self.attempts.append({
                    'url': login_url,
                    'status': response.status_code,
                    'msg': "OK" if response.status_code == 200 else "Failed",
                    'resp_sample': resp_text
                })
                print(f"DEBUG: Trying SAS URL: {login_url} | Status: {response.status_code}")
                
                if response.status_code == 200:
                    try:
                        token = json.loads(response.content).get('token')
                        if token:
                            print(f"DEBUG: Successfully found working API base: {variant}")
                            self.base_url = variant # Save for future calls
                            return token, None
                    except:
                        # 200 but not JSON (likely HTML or PHP warning)
                        last_error = f"200 OK but not JSON: {resp_text}"
                        self.attempts[-1]['error_detail'] = last_error
                        continue
                
                # If not 200, capture message for debugging
                try:
                    error_data = json.loads(response.content)
                    last_error = error_data.get('message') or error_data.get('error') or f"Status {response.status_code}"
                except:
                    last_error = f"Status {response.status_code}"
                
                self.attempts[-1]['error_detail'] = last_error
                
                # If it's a real auth failure (403/401) and we have a message, it might be the right URL
                if response.status_code in [401, 403] and 'user' in last_error.lower() or 'password' in last_error.lower():
                    # If the message mentions user/password, this is definitely the right endpoint
                    self.base_url = variant
                    return None, last_error
                    
            except Exception as e:
                error_msg = str(e)
                self.attempts.append({
                    'url': login_url,
                    'status': 'ERR',
                    'msg': error_msg
                })
                print(f"DEBUG: Error with {login_url}: {e}")
                last_error = error_msg
        
        return None, last_error

    def post(self, token, route, payload):
        url = self.base_url + route
        self.session.headers['Authorization'] = f'Bearer {token}'
        data = {'payload': payload}
        req = self.session.post(url, json=data)
        return req.json() if req.status_code == 200 else req.status_code

    def details(self, token):
        # Match routes with the found base_url structure
        route = 'user'
        url = self.base_url + route
        self.session.headers['Authorization'] = f'Bearer {token}'
        response = self.session.get(url)
        if response.status_code == 200:
            return response.json()
        return response.status_code
