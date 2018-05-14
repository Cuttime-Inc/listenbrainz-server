
import os
import re
import ujson
import zipfile
from datetime import datetime
from os import path, makedirs
from time import time

from flask import Blueprint, render_template, request, url_for, redirect, current_app
from flask import make_response
from flask_login import current_user, login_required
import spotipy.oauth2
from werkzeug.exceptions import NotFound, BadRequest, RequestEntityTooLarge, InternalServerError
from werkzeug.utils import secure_filename

import listenbrainz.db.user as db_user
import listenbrainz.db.spotify as db_spotify
from listenbrainz import webserver
from listenbrainz.db.exceptions import DatabaseException
from listenbrainz.webserver import flash
from listenbrainz.webserver.redis_connection import _redis
from listenbrainz.webserver.utils import sizeof_readable
from listenbrainz.webserver.views.api_tools import convert_backup_to_native_format, insert_payload, validate_listen, \
    LISTEN_TYPE_IMPORT

profile_bp = Blueprint("profile", __name__)


EXPORT_FETCH_COUNT = 5000


@profile_bp.route("/resettoken", methods=["GET", "POST"])
@login_required
def reset_token():
    if request.method == "POST":
        token = request.form.get("token")
        if token != current_user.auth_token:
            raise BadRequest("Can only reset token of currently logged in user")
        reset = request.form.get("reset")
        if reset == "yes":
            try:
                db_user.update_token(current_user.id)
                flash.info("Access token reset")
            except DatabaseException:
                flash.error("Something went wrong! Unable to reset token right now.")
        return redirect(url_for("profile.info"))
    else:
        token = current_user.auth_token
        return render_template(
            "user/resettoken.html",
            token=token,
        )


@profile_bp.route("/resetlatestimportts", methods=["GET", "POST"])
@login_required
def reset_latest_import_timestamp():
    if request.method == "POST":
        token = request.form.get("token")
        if token != current_user.auth_token:
            raise BadRequest("Can only reset latest import timestamp of currently logged in user")
        reset = request.form.get("reset")
        if reset == "yes":
            try:
                db_user.reset_latest_import(current_user.musicbrainz_id)
                flash.info("Latest import time reset, we'll now import all your data instead of stopping at your last imported listen.")
            except DatabaseException:
                flash.error("Something went wrong! Unable to reset latest import timestamp right now.")
        return redirect(url_for("profile.info"))
    else:
        token = current_user.auth_token
        return render_template(
            "profile/resetlatestimportts.html",
            token=token,
        )


@profile_bp.route("/")
@login_required
def info():
    return render_template(
        "profile/info.html",
        user=current_user
    )


@profile_bp.route("/import")
@login_required
def import_data():
    """ Displays the import page to user, giving various options """

    # Return error if LASTFM_API_KEY is not given in config.py
    if 'LASTFM_API_KEY' not in current_app.config or current_app.config['LASTFM_API_KEY'] == "":
        return NotFound("LASTFM_API_KEY not specified.")

    return render_template(
        "user/import.html",
        user=current_user,
        scraper_url=url_for(
            "user.lastfmscraper",
            user_name=current_user.musicbrainz_id,
            _external=True,
        ),
    )


@profile_bp.route("/export", methods=["GET", "POST"])
@login_required
def export_data():
    """ Exporting the data to json """
    if request.method == "POST":
        db_conn = webserver.create_influx(current_app)
        filename = current_user.musicbrainz_id + "_lb-" + datetime.today().strftime('%Y-%m-%d') + ".json"

        # fetch all listens for the user from listenstore by making repeated queries to
        # listenstore until we get all the data
        to_ts = int(time())
        listens = []
        while True:
            batch = db_conn.fetch_listens(current_user.musicbrainz_id, to_ts=to_ts, limit=EXPORT_FETCH_COUNT)
            if not batch:
                break
            listens.extend(batch)
            to_ts = batch[-1].ts_since_epoch  # new to_ts will the the timestamp of the last listen fetched

        # Fetch output and convert it into dict with keys as indexes
        output = []
        for index, obj in enumerate(listens):
            dic = obj.data
            dic['timestamp'] = obj.ts_since_epoch
            dic['release_msid'] = None if obj.release_msid is None else str(obj.release_msid)
            dic['artist_msid'] = None if obj.artist_msid is None else str(obj.artist_msid)
            dic['recording_msid'] = None if obj.recording_msid is None else str(obj.recording_msid)
            output.append(dic)

        response = make_response(ujson.dumps(output))
        response.headers["Content-Disposition"] = "attachment; filename=" + filename
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
        response.mimetype = "text/json"
        return response
    else:
        return render_template("user/export.html", user=current_user)


@profile_bp.route("/upload", methods=['GET', 'POST'])
@login_required
def upload():
    if request.method == 'POST':
        try:
            f = request.files['file']
            if f.filename == '':
                flash.warning('No file selected.')
                return redirect(request.url)
        except RequestEntityTooLarge:
            raise RequestEntityTooLarge('Maximum filesize upload limit exceeded. File must be <=' +
                                        sizeof_readable(current_app.config['MAX_CONTENT_LENGTH']))
        except:
            raise InternalServerError("Something went wrong. Could not upload the file")

        # Check upload folder
        if 'UPLOAD_FOLDER' not in current_app.config:
            raise InternalServerError("Could not upload the file. Upload folder not specified")
        upload_path = path.join(path.abspath(current_app.config['UPLOAD_FOLDER']), current_user.musicbrainz_id)
        if not path.isdir(upload_path):
            makedirs(upload_path)

        # Write to a file
        filename = path.join(upload_path, secure_filename(f.filename))
        f.save(filename)

        if not zipfile.is_zipfile(filename):
            raise BadRequest('Not a valid zip file.')

        success = failure = 0
        regex = re.compile('json/scrobbles/scrobbles-*')
        try:
            zf = zipfile.ZipFile(filename, 'r')
            files = zf.namelist()
            # Iterate over file that match the regex
            for f in [f for f in files if regex.match(f)]:
                try:
                    # Load listens file
                    jsonlist = ujson.loads(zf.read(f))
                    if not isinstance(jsonlist, list):
                        raise ValueError
                except ValueError:
                    failure += 1
                    continue

                payload = convert_backup_to_native_format(jsonlist)
                for listen in payload:
                    validate_listen(listen, LISTEN_TYPE_IMPORT)
                insert_payload(payload, current_user)
                success += 1
        except Exception:
            raise BadRequest('Not a valid lastfm-backup-file.')
        finally:
            os.remove(filename)

        # reset listen count for user
        db_connection = webserver.influx_connection._influx
        db_connection.reset_listen_count(current_user.musicbrainz_id)

        flash.info('Congratulations! Your listens from %d files have been uploaded successfully.' % success)
    return redirect(url_for("profile.import_data"))


@profile_bp.route('/connect-spotify', methods=['GET', 'POST'])
@login_required
def connect_spotify():
    if request.method == 'POST' and request.form.get('delete') == 'yes':
        db_spotify.delete_spotify(current_user.id)
        flash.info('Your Spotify account has been unlinked')
    tokens = db_spotify.get_token_for_user(current_user.id)
    spotify_url = None
    if not tokens:
        sp_oauth = db_spotify._get_spotify_oauth()
        spotify_url = sp_oauth.get_authorize_url()

    return render_template(
        'user/spotify.html',
        account=tokens,
        spotify_login_url=spotify_url
    )


@profile_bp.route('/connect-spotify/callback')
@login_required
def connect_spotify_callback():
    code = request.args.get('code')
    if not code:
        raise BadRequest('missing code')
    sp_oauth = db_spotify._get_spotify_oauth()

    try:
        token = sp_oauth.get_access_token(code)
        print("code: {}".format(code))

        access_token = token['access_token']
        refresh_token = token['refresh_token']
        expires_at = token['expires_at']
        db_spotify.create_spotify(current_user.id, access_token, refresh_token, expires_at)
    except spotipy.oauth2.SpotifyOauthError as e:
        flash.warn('Unable to authenticate with Spotify (error {})'.format(e.args[0]))

    return redirect(url_for('profile.connect_spotify'))
