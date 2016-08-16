import json
from io import StringIO
from time import time

from django.contrib.auth.models import User
from django.core.urlresolvers import reverse
from django.test import TestCase
from django.test.utils import override_settings
from mock import Mock, patch

from pipeline import models
from pipeline.tests.utils import override_plugin_backend
from pipeline.tests import factories


class VideosUnauthenticatedTests(TestCase):

    def test_list_videos(self):
        url = reverse("api:v1:video-list")
        response = self.client.get(url)
        # BasicAuthentication returns a 401 in case
        self.assertEqual(401, response.status_code)


class BaseAuthenticatedTests(TestCase):

    def setUp(self):
        self.user = User.objects.create(username="test", is_active=True)
        self.user.set_password("password")
        self.user.save()
        self.client.login(username="test", password="password")

    def create_video(self, **kwargs):
        return factories.VideoFactory(**kwargs)


class VideoUploadUrlTests(BaseAuthenticatedTests):

    def test_obtain_video_upload_url(self):
        url = reverse("api:v1:videoupload-list")

        create_upload_url = Mock(return_value={
            'url': 'http://example.com',
            'method': 'POST',
            'id': 'videoid',
            'expires_at': 0,
        })
        with override_plugin_backend(create_upload_url=create_upload_url):
            response = self.client.post(url, {'filename': 'Some file.mp4'})

        self.assertEqual(200, response.status_code)
        upload_url = response.json()
        create_upload_url.assert_called_once_with('Some file.mp4')
        self.assertIn("url", upload_url)
        self.assertEqual("http://example.com", upload_url["url"])
        self.assertIn("method", upload_url)
        self.assertEqual("POST", upload_url["method"])
        self.assertEqual(1, models.VideoUploadUrl.objects.count())
        self.assertEqual(self.user, models.VideoUploadUrl.objects.get().owner)


    def test_get_fails_on_videoupload(self):
        url = reverse("api:v1:videoupload-list")
        response = self.client.get(url)
        self.assertEqual(405, response.status_code) # method not allowed


class VideosTests(BaseAuthenticatedTests):

    # queries:
    # 1) django session
    # 2) user authentication
    # 3) video + transcoding job
    VIDEOS_LIST_NUM_QUERIES_EMPTY_RESULT = 3
    # 4) subtitles prefetch
    # 5) formats prefetch
    VIDEOS_LIST_NUM_QUERIES = VIDEOS_LIST_NUM_QUERIES_EMPTY_RESULT + 2

    def test_list_videos(self):
        url = reverse("api:v1:video-list")
        with self.assertNumQueries(self.VIDEOS_LIST_NUM_QUERIES_EMPTY_RESULT):
            response = self.client.get(url)
        videos = response.json()

        self.assertEqual(200, response.status_code)
        self.assertEqual([], videos)

    def test_get_video(self):
        video = self.create_video(public_id="videoid", title="Some title")
        models.VideoTranscoding(video=video, status=models.VideoTranscoding.STATUS_SUCCESS)
        response = self.client.get(reverse('api:v1:video-detail', kwargs={'id': 'videoid'}))
        self.assertEqual(200, response.status_code)
        video = response.json()

        self.assertEqual('videoid', video['id'])
        self.assertEqual('Some title', video['title'])
        self.assertEqual([], video['subtitles'])
        self.assertEqual([], video['formats'])

    def test_get_not_processing_video(self):
        self.create_video(public_id="videoid", title='videotitle')
        url = reverse("api:v1:video-list")
        videos = self.client.get(url).json()

        self.assertEqual(1, len(videos))
        self.assertEqual('videoid', videos[0]['id'])
        self.assertEqual('videotitle', videos[0]['title'])
        self.assertIn('status_details', videos[0])
        self.assertEqual(None, videos[0]['status_details'])

    def test_get_processing_video(self):
        video = self.create_video(public_id="videoid", title='videotitle')
        _transcoding = models.VideoTranscoding.objects.create(
            video=video,
            progress=42,
            status=models.VideoTranscoding.STATUS_PROCESSING
        )
        videos = self.client.get(reverse("api:v1:video-list")).json()

        self.assertEqual('processing', videos[0]['status_details']['status'])
        self.assertEqual(42, videos[0]['status_details']['progress'])

    def test_list_failed_videos(self):
        video = self.create_video(public_id="videoid", title='videotitle')
        _transcoding = models.VideoTranscoding.objects.create(
            video=video,
            status=models.VideoTranscoding.STATUS_FAILED
        )

        videos = self.client.get(reverse("api:v1:video-list")).json()
        self.assertEqual([], videos)

    def test_create_video_fails(self):
        url = reverse("api:v1:video-list")
        response = self.client.post(
            url,
            {
                "public_id": "videoid",
                "title": "sometitle"
            }
        )
        self.assertEqual(405, response.status_code) # method not allowed

    @override_plugin_backend(
        get_uploaded_video=lambda video_id: None,
        create_transcoding_jobs=lambda video_id: [],
        iter_available_formats=lambda video_id: [],
    )
    def test_get_video_that_was_just_uploaded(self):
        factories.VideoUploadUrlFactory(
            public_video_id="videoid",
            expires_at=time() + 3600
        )
        response = self.client.get(reverse("api:v1:video-detail", kwargs={'id': 'videoid'}))

        self.assertEqual(200, response.status_code)

    def test_update_video_title(self):
        self.create_video(public_id="videoid", title='videotitle')
        response = self.client.put(
            reverse('api:v1:video-detail', kwargs={'id': 'videoid'}),
            data=json.dumps({'title': 'title2'}),
            content_type='application/json'
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual('title2', models.Video.objects.get().title)

    def test_delete_video(self):
        mock_delete_resources = Mock()
        self.create_video(public_id="videoid")
        with override_plugin_backend(delete_resources=mock_delete_resources):
            response = self.client.delete(reverse('api:v1:video-detail', kwargs={'id': 'videoid'}))

        self.assertEqual(204, response.status_code)
        self.assertEqual(0, models.Video.objects.count())
        mock_delete_resources.assert_called_once_with('videoid')

    @override_plugin_backend(
        get_subtitles_download_url=lambda vid, sid, lang: "http://example.com/{}.vtt".format(sid)
    )
    def test_get_video_with_subtitles(self):
        video = self.create_video(public_id="videoid")
        video.subtitles.create(language="fr", public_id="subid1")
        video.subtitles.create(language="en", public_id="subid2")

        with self.assertNumQueries(self.VIDEOS_LIST_NUM_QUERIES):
            video = self.client.get(reverse("api:v1:video-detail", kwargs={'id': 'videoid'})).json()

        self.assertEqual([
            {
                'id': 'subid1',
                'language': 'fr',
                'download_url': 'http://example.com/subid1.vtt'
            },
            {
                'id': 'subid2',
                'language': 'en',
                'download_url': 'http://example.com/subid2.vtt'
            },
        ], video['subtitles'])


    @override_plugin_backend(
        get_video_streaming_url=lambda video_id, format_name:
            "http://example.com/{}/{}.mp4".format(video_id, format_name),
        iter_available_formats=lambda video_id: [],
    )
    def test_get_video_with_formats(self):
        video = self.create_video(public_id="videoid")
        video.formats.create(name="SD", bitrate=128)
        video.formats.create(name="HD", bitrate=256)

        with self.assertNumQueries(self.VIDEOS_LIST_NUM_QUERIES):
            video = self.client.get(reverse("api:v1:video-detail", kwargs={'id': 'videoid'})).json()

        self.assertEqual([
            {
                'name': 'SD',
                'streaming_url': 'http://example.com/videoid/SD.mp4'
            },
            {
                'name': 'HD',
                'streaming_url': 'http://example.com/videoid/HD.mp4'
            },
        ], video['formats'])


class VideoSubtitlesTests(BaseAuthenticatedTests):

    @override_plugin_backend(
        upload_subtitles=lambda *args: None,
        get_subtitles_download_url=lambda *args: "http://example.com/subs.vtt"
    )
    def test_upload_subtitles(self):
        video = self.create_video(public_id="videoid")
        url = reverse("api:v1:video-subtitles", kwargs={'id': 'videoid'})
        subfile = StringIO("some srt content in here")
        response = self.client.post(
            url,
            data={
                'language': 'fr',
                'name': 'subs.srt', 'attachment': subfile
            },
        )

        self.assertEqual(201, response.status_code)
        subtitles = response.json()
        self.assertLess(0, len(subtitles["id"]))
        self.assertEqual("fr", subtitles["language"])
        self.assertEqual("http://example.com/subs.vtt", subtitles["download_url"])
        self.assertEqual(1, video.subtitles.count())

    def test_upload_subtitles_invalid_language(self):
        self.create_video(public_id="videoid")
        url = reverse("api:v1:video-subtitles", kwargs={'id': 'videoid'})
        # only country codes are accepted
        response = self.client.post(url, data={'language': 'french'})

        self.assertEqual(400, response.status_code)
        self.assertIn('language', response.json())
        self.assertEqual(0, models.VideoSubtitles.objects.count())

    def test_upload_subtitles_missing_attachment(self):
        self.create_video(public_id="videoid")
        url = reverse("api:v1:video-subtitles", kwargs={'id': 'videoid'})
        response = self.client.post(url, data={'language': 'fr'})

        self.assertEqual(400, response.status_code)
        self.assertIn('attachment', response.json())
        self.assertEqual(0, models.VideoSubtitles.objects.count())

    @patch('django.core.handlers.base.logger')# mute request logger
    def test_upload_subtitles_failed_upload(self, mock_logger):
        self.create_video(public_id="videoid")
        url = reverse("api:v1:video-subtitles", kwargs={'id': 'videoid'})
        subfile = StringIO("some srt content in here")

        upload_subtitles = Mock(side_effect=ValueError)
        with override_plugin_backend(upload_subtitles=upload_subtitles):
            self.assertRaises(ValueError, self.client.post, url,
                data={
                    'language': 'fr',
                    'name': 'subs.srt', 'attachment': subfile
                },
            )

        self.assertEqual(0, models.VideoSubtitles.objects.count())

    def test_cannot_modify_subtitles(self):
        video = self.create_video(public_id="videoid")
        video.subtitles.create(public_id="subsid", language="fr")
        url = reverse("api:v1:video-subtitles", kwargs={'id': 'videoid'})
        subfile = StringIO("some srt content in here")

        with override_plugin_backend(
                upload_subtitles=lambda *args: None,
                get_subtitles_download_url=lambda *args: None
        ):
            response = self.client.post(url, data={
                'id': 'subsid',
                'language': 'en',
                'name': 'subs.srt', 'attachment': subfile
            })

        # Subtitles were in fact created, not modified
        self.assertEqual(201, response.status_code)
        self.assertEqual('fr', video.subtitles.get(public_id='subsid').language)
        self.assertEqual('en', video.subtitles.exclude(public_id='subsid').get().language)

    @override_settings(SUBTITLES_MAX_BYTES=139)
    def test_upload_subtitles_too_large(self):
        self.create_video(public_id="videoid")
        content = (
            "Some vtt content here. This should as long as a tweet, at"
            " 140 characters exactly. Yes, I counted, multiple times and came up with"
            " 140 chars."
        )
        subfile = StringIO(content)
        self.assertEqual(140, len(content))
        url = reverse("api:v1:video-subtitles", kwargs={'id': 'videoid'})

        response = self.client.post(url, data={
            'language': 'en',
            'name': 'subs.srt', 'attachment': subfile
        })
        self.assertEqual(400, response.status_code)
        self.assertIn('attachment', response.json())
        self.assertIn('139', response.json()['attachment'])
