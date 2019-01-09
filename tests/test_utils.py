"""Tests for functions and classes implemented in 'utils' module."""

import datetime
import pytest
import semantic_version as sv
import os
import json
from unittest.mock import Mock, patch

from bayesian.utils import (
    do_projection,
    fetch_file_from_github,
    is_valid, has_field, get_user_email,
    convert_version_to_proper_semantic as cvs,
    version_info_tuple as vt,
    select_latest_version as slv,
    GremlinComponentAnalysisResponse,
    CveByDateEcosystemUtils,
    resolved_files_exist,
    get_ecosystem_from_manifest
)
from f8a_worker.enums import EcosystemBackend
from f8a_worker.models import Analysis, Ecosystem, Package, Version, WorkerResult
from urllib.request import urlopen

now = datetime.datetime.now()
later = now + datetime.timedelta(minutes=10)

mocker_input = {
    "result": {
        "data": [
            {
                "cve": {
                    "ecosystem": ["maven"],
                    "cve_id": ["CVE-2018-0001"],
                    "cvss_v2": [10.0],
                    "nvd_status": ["Awaiting Analyses"],
                    "description": ["Some description here updated just now."],
                    "modified_date": ["20180911"]
                },
                "epv": {
                    "pname": ["io.vertx:vertx-core"],
                    "version": ["3.4.1"],
                    "pecosystem": ["maven"],
                }
            },
            {
                "ecosystem": ["npm"],
                "cve_id": ["CVE-2018-0002"],
                "cvss_v2": [10.0],
                "nvd_status": ["Depricated"],
                "description": ["Some description here updated just now."],
                "modified_date": ["20180911"]

            }
        ]
    }
}


@pytest.fixture
def analyses(app):
    """Prepare the known set of data used by tests."""
    e1 = Ecosystem(name='npm', backend=EcosystemBackend.npm)
    p1 = Package(ecosystem=e1, name='arrify')
    v1 = Version(package=p1, identifier='1.0.1')
    model1 = Analysis(version=v1, started_at=now, finished_at=later)
    app.rdb.session.add(model1)

    e2 = Ecosystem(name='pypi', backend=EcosystemBackend.pypi)
    p2 = Package(ecosystem=e2, name='flexmock')
    v2 = Version(package=p2, identifier='0.10.1')
    model2 = Analysis(version=v2, started_at=later, access_count=1)
    app.rdb.session.add(model2)
    app.rdb.session.commit()

    worker_results2 = {'a': 'b', 'c': 'd', 'e': 'f', 'g': 'h', 'i': 'j',
                       'digests': {'details':
                                   [{'artifact': True,
                                     'sha1': '6be7ae55bae2372c7be490321bbe5ead278bb51b'}]}}
    for w, tr in worker_results2.items():
        app.rdb.session.add(WorkerResult(analysis_id=model2.id, worker=w, task_result=tr))

    model3 = Analysis(version=v2, started_at=later, access_count=1,
                      audit={'audit': {'audit': 'audit', 'e': 'f', 'g': 'h'}, 'a': 'b', 'c': 'd'})
    app.rdb.session.add(model3)
    app.rdb.session.commit()
    worker_results3 = {'digests': {'details':
                                   [{'artifact': True,
                                     'sha1': '6be7ae55bae2372c7be490321bbe5ead278bb51b'}]}}
    for w, tr in worker_results3.items():
        app.rdb.session.add(WorkerResult(analysis_id=model3.id, worker=w, task_result=tr))
    app.rdb.session.commit()
    return (model1, model2, model3)


@pytest.mark.usefixtures('rdb')
class TestDoProjection(object):
    """Tests for the function 'do_projection' implemented in 'utils' module."""

    def test_empty_projection(self, analyses):
        """Test that no fields are returned for empty projection."""
        projection = []
        expected = {}
        result = do_projection(projection, analyses[0])
        assert expected == result

    def test_simple_projection(self, analyses):
        """Test simple projection of 2 simple arguments."""
        projection = ['ecosystem', 'package']
        # pypi has order 1
        expected = {'ecosystem': 'npm', 'package': 'arrify'}
        returned = do_projection(projection, analyses[0])
        assert expected == returned

    def test_none_projection(self, analyses):
        """Check that original model is returned if projection is None."""
        projection = None
        returned = do_projection(projection, analyses[0])
        expected = analyses[0].to_dict()
        assert expected == returned

    def test_nested_projection(self, analyses):
        """Test whether filtering of nested JSON returns just desired field."""
        projection = ['analyses.digests']
        expected = {'analyses': {'digests': {'details':
                                             [{'artifact': True, 'sha1':
                                               '6be7ae55bae2372c7be490321bbe5ead278bb51b'}]}}}
        result = do_projection(projection, analyses[1])
        assert expected == result

    def test_combined_projection(self, analyses):
        """Combining simple fields with nested fields."""
        projection = ['analyses.digests', 'analyses.a', 'package']
        expected = {'analyses': {'a': 'b', 'digests': {
            'details': [{'artifact': True, 'sha1': '6be7ae55bae2372c7be490321bbe5ead278bb51b'}]}},
                    'package': 'flexmock'}
        result = do_projection(projection, analyses[1])
        assert expected == result

    def test_three_level_fields(self, analyses):
        """Testing third level of nested JSON."""
        projection = ['analyses.digests.details', 'audit.audit.audit']
        expected = {'audit': {'audit': {'audit': 'audit'}},
                    'analyses':
                    {'digests': {'details':
                                 [{'artifact': True,
                                     'sha1': '6be7ae55bae2372c7be490321bbe5ead278bb51b'}]}}}
        result = do_projection(projection, analyses[2])
        assert expected == result


class TestFetchFileFromGithub:
    """Tests for the function 'fetch_file_from_github' implemented in 'utils' module."""

    __url = 'https://github.com/ravsa/testManifest'

    def test_github_url_exist_or_not(self):
        """Check for github repo exist or not."""
        assert urlopen(
            self.__url).code == 200, "Not able to access the url {}".format(self.__url)

    def test_repo_with_file_exist(self):
        """Check wheather file exist in github repo."""
        file_name = 'pom.xml'
        result = fetch_file_from_github(self.__url, file_name)
        assert not bool(
            {'filename', 'filepath', 'content'}.symmetric_difference(result[0].keys()))

    def test_repo_file_in_diff_branch(self):
        """Check for file exist in specific branch or not."""
        file_name = 'pom.xml'
        branch_name = 'dev-test-branch'
        result = fetch_file_from_github(
            self.__url, file_name, branch=branch_name)
        assert not bool(
            {'filename', 'filepath', 'content'}.symmetric_difference(result[0].keys()))


def test_semantic_versioning():
    """Check the function cvs()."""
    package_name = "test_package"
    version = "-1"
    assert cvs(version, package_name) == sv.Version("0.0.0")
    version = ""
    assert cvs(version, package_name) == sv.Version("0.0.0")
    version = None
    assert cvs(version, package_name) == sv.Version("0.0.0")
    version = "1.5.2.RELEASE"
    assert cvs(version, package_name) == sv.Version("1.5.2+RELEASE")
    version = "1.5-2.RELEASE"
    assert cvs(version, package_name) == sv.Version("1.5.2+RELEASE")
    version = "2"
    assert cvs(version, package_name) == sv.Version("2.0.0")
    version = "2.3"
    assert cvs(version, package_name) == sv.Version("2.3.0")
    version = "2.0.rc1"
    assert cvs(version, package_name) == sv.Version("2.0.0+rc1")


def test_version_info_tuple():
    """Check the function vt()."""
    # TODO: reduce cyclomatic complexity
    version_str = "2.0.rc1"
    package_name = "test_package"
    version_obj = cvs(version_str, package_name)
    version_info = vt(version_obj)
    assert len(version_info) == 4
    assert version_info[0] == version_obj.major
    assert version_info[1] == version_obj.minor
    assert version_info[2] == version_obj.patch
    assert version_info[3] == version_obj.build
    version_obj = ""
    version_info = vt(version_obj)
    assert len(version_info) == 4
    assert version_info[0] == 0
    assert version_info[1] == 0
    assert version_info[2] == 0
    assert version_info[3] == tuple()


def test_select_latest_version():
    """Check fucntion slv()."""
    latest = "1.2.2"
    libio = "1.2.3"
    package_name = "test_package"
    result_version = slv(latest, libio, package_name)
    assert result_version == libio
    latest = ""
    libio = ""
    result_version = slv(latest, libio, package_name)
    assert result_version == ""


def test_get_user_email():
    """Check the function get_user_email()."""
    assert get_user_email(None) == 'bayesian@redhat.com'

    user_profile = {'email': 'xyzzy'}
    assert get_user_email(user_profile) == 'xyzzy'

    user_profile = {'not-email': 'xyzzy'}
    assert get_user_email(user_profile) == 'bayesian@redhat.com'


def test_has_field():
    """Check the function has_field()."""
    assert has_field({}, [])
    assert not has_field({}, ["field1", "field2"])
    assert has_field({"field1": 42}, ["field1"])
    assert has_field({"field1": {"field2": 42}}, ["field1", "field2"])


def test_is_valid():
    """Check the function is_valid()."""
    assert not is_valid(None)
    assert is_valid("parameter")


def test_gremlin_component_analysis_response():
    """Test GremlinComponentAnalysisResponse."""
    rest_json_path = os.path.join(
        os.path.dirname(__file__),
        'data/gremlin/component_analysis_response'
    )
    with open(rest_json_path) as f:
        resp_json = json.load(f)
        data = resp_json['result']['data']

        resp = GremlinComponentAnalysisResponse(
            'commons-fileupload:commons-fileupload', '1.2', data
        )

        assert resp.has_cves(), str(resp._cves)
        assert resp.get_max_cvss_score() == 3.3
        assert resp.get_version_without_cves() == '1.3.3'
        assert len(resp.get_cve_maps()) == 1


@patch("bayesian.utils.post")
def test_cve_get_by_date_valid(mocker):
    """Test getting CVEs for (date)."""
    mocker.return_value = mock_response = Mock()
    mock_response.json.return_value = mocker_input

    cve = CveByDateEcosystemUtils('20160911')
    response = cve.get_cves_by_date()

    assert response
    assert 'add' in response
    assert response['add'][0]['cve_id'] == 'CVE-2018-0001'
    assert 'ecosystem' in response['add'][0]
    assert 'name' in response['add'][0]
    assert 'version' in response['add'][0]


@patch("bayesian.utils.post")
def test_get_cves_by_date_ecosystem_add(mocker):
    """Test getting CVEs by date and ecosystem."""
    mocker.return_value = mock_response = Mock()
    mock_response.json.return_value = mocker_input

    cve = CveByDateEcosystemUtils('20180911', 'maven')
    response = cve.get_cves_by_date_ecosystem()

    assert response
    assert 'add' in response
    assert response['add'][0]['cve_id'] == 'CVE-2018-0001'
    assert 'ecosystem' in response['add'][0]
    assert response['add'][0]['ecosystem'] == 'maven'
    assert 'name' in response['add'][0]
    assert 'version' in response['add'][0]


@patch("bayesian.utils.post")
def test_get_cves_by_date_ecosystem_remove(mocker):
    """Test getting CVEs by date and ecosystem."""
    mocker.return_value = mock_response = Mock()
    mock_response.json.return_value = mocker_input

    cve = CveByDateEcosystemUtils('20180911', 'npm')
    response = cve.get_cves_by_date_ecosystem()

    assert response
    assert 'remove' in response
    assert response['remove'][0]['cve_id'] == 'CVE-2018-0002'
    assert 'ecosystem' in response['remove'][0]
    assert response['remove'][0]['ecosystem'] == 'npm'


def test_check_manifest_for_resolved_deps():
    """Test test_check_manifest_for_resolved_deps function."""
    manifests = [
        {
            'filename': "npmlist.json",
            'ecosystem': "npm"
        },
        {
            'filename': "npm-abcd.json",
            'ecosystem': "npm"
        }
    ]
    resp = resolved_files_exist(manifests)
    assert resp is True

    manifests = [
        {
            'filename': "npm-list.json",
            'ecosystem': "npm"
        },
        {
            'filename': "npm-abcd.json",
            'ecosystem': "npm"
        }
    ]
    resp = resolved_files_exist(manifests)
    assert resp is False

    resp = resolved_files_exist("dependencies.txt")
    assert resp is True


def test_get_ecosystem_from_manifest():
    """Test get_ecosystem_from_manifest function."""
    manifests = [
        {
            'filename': "npmlist.json",
            'ecosystem': "npm"
        },
        {
            'filename': "npm-abcd.json",
            'ecosystem': "npm"
        }
    ]
    resp = get_ecosystem_from_manifest(manifests)
    assert resp == "npm"

    manifests = [
        {
            'filename': "dependencies.txt",
            'ecosystem': "maven"
        }
    ]
    resp = get_ecosystem_from_manifest(manifests)
    assert resp == "maven"

    resp = get_ecosystem_from_manifest("dependencies.txt")
    assert resp == "maven"
