import json
from time import time

from django.core.urlresolvers import reverse
from django.test import TestCase
from mock import Mock

from pipeline import models
from pipeline.tests.utils import override_plugin_backend
from pipeline.tests import factories
from .base import BaseAuthenticatedTests


class VideosUnauthenticatedTests(TestCase):

    def test_list_videos(self):
        url = reverse("api:v1:video-list")
        response = self.client.get(url)
        # BasicAuthentication returns a 401 in case
        self.assertEqual(401, response.status_code)


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

    def test_list_videos_with_different_owners(self):
        video1 = factories.VideoFactory(owner=self.user)
        _video2 = factories.VideoFactory(owner=factories.UserFactory())
        videos = self.client.get(reverse("api:v1:video-list")).json()

        self.assertEqual(1, len(videos))
        self.assertEqual(video1.public_id, videos[0]["id"])

    def test_get_video(self):
        video = factories.VideoFactory(public_id="videoid", title="Some title", owner=self.user)
        models.VideoTranscoding(video=video, status=models.VideoTranscoding.STATUS_SUCCESS)
        response = self.client.get(reverse('api:v1:video-detail', kwargs={'id': 'videoid'}))
        self.assertEqual(200, response.status_code)
        video = response.json()

        self.assertEqual('videoid', video['id'])
        self.assertEqual('Some title', video['title'])
        self.assertEqual([], video['subtitles'])
        self.assertEqual([], video['formats'])

    def test_get_not_processing_video(self):
        factories.VideoFactory(public_id="videoid", title='videotitle', owner=self.user)
        url = reverse("api:v1:video-list")
        videos = self.client.get(url).json()

        self.assertEqual(1, len(videos))
        self.assertEqual('videoid', videos[0]['id'])
        self.assertEqual('videotitle', videos[0]['title'])
        self.assertIn('status_details', videos[0])
        self.assertEqual(None, videos[0]['status_details'])

    def test_get_processing_video(self):
        video = factories.VideoFactory(public_id="videoid", title='videotitle', owner=self.user)
        _transcoding = models.VideoTranscoding.objects.create(
            video=video,
            progress=42,
            status=models.VideoTranscoding.STATUS_PROCESSING
        )
        videos = self.client.get(reverse("api:v1:video-list")).json()

        self.assertEqual('processing', videos[0]['status_details']['status'])
        self.assertEqual(42, videos[0]['status_details']['progress'])

    def test_list_failed_videos(self):
        video = factories.VideoFactory(public_id="videoid", title='videotitle', owner=self.user)
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
            expires_at=time() + 3600,
            owner=self.user
        )
        response = self.client.get(reverse("api:v1:video-detail", kwargs={'id': 'videoid'}))

        self.assertEqual(200, response.status_code)

    def test_update_video_title(self):
        factories.VideoFactory(public_id="videoid", title='videotitle', owner=self.user)
        response = self.client.put(
            reverse('api:v1:video-detail', kwargs={'id': 'videoid'}),
            data=json.dumps({'title': 'title2'}),
            content_type='application/json'
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual('title2', models.Video.objects.get().title)

    def test_delete_video(self):
        mock_delete_resources = Mock()
        factories.VideoFactory(public_id="videoid", owner=self.user)
        with override_plugin_backend(delete_resources=mock_delete_resources):
            response = self.client.delete(reverse('api:v1:video-detail', kwargs={'id': 'videoid'}))

        self.assertEqual(204, response.status_code)
        self.assertEqual(0, models.Video.objects.count())
        mock_delete_resources.assert_called_once_with('videoid')

    @override_plugin_backend(
        get_subtitle_download_url=lambda vid, sid, lang: "http://example.com/{}.vtt".format(sid)
    )
    def test_get_video_with_subtitles(self):
        video = factories.VideoFactory(public_id="videoid", owner=self.user)
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
        video = factories.VideoFactory(public_id="videoid", owner=self.user)
        video.formats.create(name="SD", bitrate=128)
        video.formats.create(name="HD", bitrate=256)

        with self.assertNumQueries(self.VIDEOS_LIST_NUM_QUERIES):
            video = self.client.get(reverse("api:v1:video-detail", kwargs={'id': 'videoid'})).json()

        self.assertEqual([
            {
                'name': 'SD',
                'streaming_url': 'http://example.com/videoid/SD.mp4',
                'bitrate': 128
            },
            {
                'name': 'HD',
                'streaming_url': 'http://example.com/videoid/HD.mp4',
                'bitrate': 256
            },
        ], video['formats'])

    def test_list_videos_in_playlist(self):
        playlist = factories.PlaylistFactory(name="Funkadelic playlist", owner=self.user)
        video_in_playlist = factories.VideoFactory(owner=self.user)
        _video_not_in_playlist = factories.VideoFactory(owner=self.user)
        playlist.videos.add(video_in_playlist)

        response = self.client.get(reverse('api:v1:video-list'), data={'playlist_id': playlist.public_id})
        videos = response.json()

        self.assertEqual(1, len(videos))
        self.assertEqual(video_in_playlist.public_id, videos[0]["id"])
