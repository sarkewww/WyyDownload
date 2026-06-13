"""Test /qq/song endpoint"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from main import app


def test_qq_song_json():
    with app.test_client() as client:
        resp = client.post('/qq/song', data={
            'id': 'https://i.y.qq.com/v8/playsong.html?songid=591049455#webchat_redirect',
            'level': 'flac',
            'type': 'json'
        })
        print(f"Status: {resp.status_code}")
        data = resp.get_json()
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return data


if __name__ == '__main__':
    test_qq_song_json()
