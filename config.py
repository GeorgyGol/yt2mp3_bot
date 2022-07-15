from os import environ, path

BOT_TOKEN = environ['BOT_TOKEN']
REDIS_HOST = environ['REDIS_HOST']

BASE_DIR = path.abspath(path.dirname(__file__))
BASE_MP3_PATH = path.join(BASE_DIR, 'FILES/')