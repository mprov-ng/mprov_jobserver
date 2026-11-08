"""
bmc-rotate (#341, mprov_control_center#486): change, check or recover a
BMC's credentials.  The mPCC stores only the pair this reports it just
logged in with, so every outcome is checked here, and that no password
ever reaches a log, an argv or the environment.
"""
import json
import os
import stat
import subprocess

import pytest
import requests

import mprov_jobserver.plugins.bmc_rotate as bmc_module
from mprov_jobserver.plugins.bmc_rotate import bmc_rotate, Ipmi, Redfish, Absent, Unreachable, scrub
from conftest import FakeResponse

CUR = {"username": "admin", "password": "old-Secret1"}
NEW = {"username": "ops", "password": "new-Secret2"}
SECRETS = [CUR["password"], NEW["password"]]


class FakeBMC:
  """ A BMC that accepts pairs in `works`, and changes them as asked. """
  method = "redfish"

  def __init__(self, works, fail_password=False, fail_rename=False, keep_password=False, keep_name=False):
    self.works = [dict(p) for p in works]
    self.fail_password, self.fail_rename = fail_password, fail_rename
    self.keep_password, self.keep_name = keep_password, keep_name
    self.calls = []

  def check_new(self, username, password):
    pass

  def login(self, pair):
    self.calls.append(("login", pair["username"]))
    return {"username": pair["username"], "password": pair["password"]} in self.works

  def set_password(self, pair, password):
    self.calls.append(("set_password", pair["username"]))
    if self.fail_password:
      raise RuntimeError(f"BMC said no to {password}")
    if not self.keep_password:
      self.works = [{"username": p["username"], "password": password} if p["username"] == pair["username"] else p
                    for p in self.works]

  def set_username(self, pair, username):
    self.calls.append(("set_username", username))
    if self.fail_rename:
      raise RuntimeError("rename refused")
    if not self.keep_name:
      self.works = [{"username": username, "password": p["password"]} if p == pair else p for p in self.works]


@pytest.fixture
def plugin(js, monkeypatch):
  js.mprovURL = "http://mpcc/"
  js.claim_job = lambda module, job: True
  monkeypatch.setattr(bmc_module.time, "sleep", lambda s: None)
  return bmc_rotate(js)


def run(plugin, monkeypatch, bmc, kind="change", new=NEW, candidates=None, tmp_path=None):
  monkeypatch.setattr(plugin, "connect", lambda address, workdir, pair: bmc)
  secret = {"id": 7, "kind": kind, "bmc": {"ipaddress": "10.9.0.5", "network": 3}, "current": dict(CUR),
            "new": dict(new) if kind == "change" else None, "candidates": candidates or []}
  return plugin.rotate(secret, str(tmp_path or "/tmp"), SECRETS + [c["password"] for c in candidates or []])


# change

def test_a_change_sets_the_password_then_the_name_and_reports_the_new_pair(plugin, monkeypatch):
  bmc = FakeBMC([CUR])
  result = run(plugin, monkeypatch, bmc)
  assert result == {"status": "succeeded", "method": "redfish", "verified": "new", "tls_verified": False, "error": ""}
  # the password first, checked, then the name
  assert [c for c in bmc.calls if c[0] != "login"] == [("set_password", "admin"), ("set_username", "ops")]
  assert bmc.works == [NEW]


def test_a_password_only_change_is_the_new_pair(plugin, monkeypatch):
  bmc = FakeBMC([CUR])
  result = run(plugin, monkeypatch, bmc, new={"username": "admin", "password": "new-Secret2"})
  assert (result["status"], result["verified"]) == ("succeeded", "new")
  assert not any(c[0] == "set_username" for c in bmc.calls)


def test_refused_current_credentials_change_nothing(plugin, monkeypatch):
  bmc = FakeBMC([{"username": "admin", "password": "something-else"}])
  result = run(plugin, monkeypatch, bmc)
  assert (result["status"], result["verified"]) == ("failed", None)
  assert "nothing was changed" in result["error"]
  assert [c[0] for c in bmc.calls] == ["login"]


def test_a_bmc_that_keeps_the_old_password_is_the_current_pair(plugin, monkeypatch):
  result = run(plugin, monkeypatch, FakeBMC([CUR], keep_password=True))
  assert (result["status"], result["verified"]) == ("failed", "current")


def test_a_failed_rename_keeps_the_new_password(plugin, monkeypatch):
  result = run(plugin, monkeypatch, FakeBMC([CUR], fail_rename=True))
  assert (result["status"], result["verified"]) == ("partial", "password_only")


def test_a_rename_that_doesnt_log_in_is_password_only(plugin, monkeypatch):
  result = run(plugin, monkeypatch, FakeBMC([CUR], keep_name=True))
  assert (result["status"], result["verified"]) == ("partial", "password_only")


def test_neither_password_logging_in_is_unconfirmed(plugin, monkeypatch):
  bmc = FakeBMC([CUR])
  bmc.set_password = lambda pair, password: setattr(bmc, "works", [])
  result = run(plugin, monkeypatch, bmc)
  assert (result["status"], result["verified"]) == ("unconfirmed", None)


def test_an_error_after_touching_the_bmc_is_unconfirmed_and_scrubbed(plugin, monkeypatch, caplog):
  result = run(plugin, monkeypatch, FakeBMC([CUR], fail_password=True))
  assert (result["status"], result["verified"]) == ("unconfirmed", None)
  assert "***" in result["error"]
  assert NEW["password"] not in result["error"]


def test_credentials_ipmi_cant_set_are_refused_before_anything_changes(plugin, monkeypatch, tmp_path):
  bmc = FakeBMC([CUR])
  bmc.method = "ipmi"
  bmc.check_new = lambda username, password: Ipmi.usable(password, 20, "password")
  result = run(plugin, monkeypatch, bmc, new={"username": "admin", "password": "has a space"})
  assert (result["status"], result["verified"]) == ("failed", None)
  assert "can't be set over IPMI" in result["error"]
  assert not any(c[0].startswith("set_") for c in bmc.calls)
  assert result["tls_verified"] is None


# check and find

def test_a_check_reports_current_when_it_logs_in(plugin, monkeypatch):
  assert run(plugin, monkeypatch, FakeBMC([CUR]), kind="check")["verified"] == "current"


def test_a_failed_check_verifies_nothing(plugin, monkeypatch):
  result = run(plugin, monkeypatch, FakeBMC([]), kind="check")
  assert (result["status"], result["verified"]) == ("failed", None)


def test_find_reports_the_first_candidate_that_logs_in(plugin, monkeypatch):
  candidates = [CUR, {"username": "admin", "password": NEW["password"]}, NEW]
  result = run(plugin, monkeypatch, FakeBMC([NEW, candidates[1]]), kind="find", candidates=candidates)
  assert (result["status"], result["verified"]) == ("succeeded", "1")


def test_find_with_none_working_verifies_nothing(plugin, monkeypatch):
  result = run(plugin, monkeypatch, FakeBMC([]), kind="find", candidates=[CUR, NEW])
  assert (result["status"], result["verified"]) == ("failed", None)


def test_an_unreachable_bmc(plugin, monkeypatch):
  def unreachable(address, workdir, pair):
    raise Unreachable("no answer over IPMI either")
  monkeypatch.setattr(plugin, "connect", unreachable)
  result = plugin.rotate({"kind": "check", "bmc": {"ipaddress": "10.9.0.5"}, "current": CUR}, "/tmp", SECRETS)
  assert (result["status"], result["verified"]) == ("unreachable", None)


def test_a_verified_bmc_certificate_is_reported(plugin, monkeypatch):
  plugin.verifyBMC = True
  assert run(plugin, monkeypatch, FakeBMC([CUR]), kind="check")["tls_verified"] is True


def test_scrub_hides_every_secret_and_caps_the_length():
  assert scrub(f"a {CUR['password']} b {NEW['password']}", SECRETS) == "a *** b ***"
  assert len(scrub("x" * 900, SECRETS)) == 500


# the job: secret, result, finish

def job(js, rotation=7):
  js.session.routes["jobs/?module=bmc-rotate&status=1"] = [{"id": 11, "params": {"rotation": rotation}}]
  js.session.routes[f"bmcrotations/{rotation}/secret/"] = {
    "id": rotation, "kind": "check", "bmc": {"ipaddress": "10.9.0.5", "network": 3}, "current": CUR,
    "new": None, "candidates": []}


def test_a_job_reports_its_result_and_finishes(js, plugin, monkeypatch, caplog):
  job(js)
  monkeypatch.setattr(plugin, "connect", lambda address, workdir, pair: FakeBMC([CUR]))
  plugin.handle_jobs()
  [(_, path, body)] = [w for w in js.session.writes if w[1].endswith("result/")]
  assert path == "bmcrotations/7/result/"
  assert body["verified"] == "current"
  assert js.job_updates_by_id[-1] == ("bmc-rotate", 4, 11)
  for secret in SECRETS:
    assert secret not in caplog.text


def test_a_job_another_job_server_claimed_is_left_alone(js, plugin, monkeypatch):
  job(js)
  js.claim_job = lambda module, job: False
  plugin.handle_jobs()
  assert ("GET", "bmcrotations/7/secret/") not in js.session.requests


def test_a_finished_request_isnt_retried(js, plugin):
  job(js)
  js.session.routes["bmcrotations/7/secret/"] = (409, {"detail": "This request is finished."})
  plugin.handle_jobs()
  assert js.job_updates_by_id[-1] == ("bmc-rotate", 4, 11)
  assert not js.session.writes


def test_a_request_that_isnt_ours_fails_the_job(js, plugin):
  job(js)
  js.session.routes["bmcrotations/7/secret/"] = (403, {"detail": "Not a job this job server is running."})
  plugin.handle_jobs()
  assert js.job_updates_by_id[-1] == ("bmc-rotate", 3, 11)


def test_an_undelivered_result_is_retried_then_fails_the_job(js, plugin, monkeypatch, caplog):
  job(js)
  monkeypatch.setattr(plugin, "connect", lambda address, workdir, pair: FakeBMC([CUR]))
  js.session.write_routes[("POST", "bmcrotations/7/result/")] = (502, {})
  plugin.handle_jobs()
  assert sum(1 for w in js.session.writes if w[1] == "bmcrotations/7/result/") == 5
  assert js.job_updates_by_id[-1] == ("bmc-rotate", 3, 11)
  assert "result not delivered" in caplog.text


def test_a_job_without_a_request_fails(js, plugin):
  js.session.routes["jobs/?module=bmc-rotate&status=1"] = [{"id": 11, "params": {}}]
  plugin.handle_jobs()
  assert js.job_updates_by_id[-1] == ("bmc-rotate", 3, 11)


def test_the_bmc_workspace_is_private_and_removed(js, plugin, monkeypatch):
  job(js)
  seen = {}

  def connect(address, workdir, pair):
    seen["mode"] = stat.S_IMODE(os.stat(workdir).st_mode)
    seen["dir"] = workdir
    return FakeBMC([CUR])
  monkeypatch.setattr(plugin, "connect", connect)
  plugin.handle_jobs()
  assert seen["mode"] == 0o700
  assert not os.path.exists(seen["dir"])


# IPMI: never on argv or in the environment

@pytest.fixture
def ipmitool(monkeypatch, tmp_path):
  calls = []

  def fake_run(argv, **kwargs):
    files = {}
    for i, arg in enumerate(argv):
      if arg in ("-f", "exec") and i + 1 < len(argv):
        path = argv[i + 1]
        files[arg] = (open(path).read(), stat.S_IMODE(os.stat(path).st_mode))
    calls.append({"argv": list(argv), "env": kwargs.get("env"), "files": files})
    if argv[-3:] == ["user", "list", "1"]:
      return subprocess.CompletedProcess(argv, 0, "ID  Name\n2   admin  true\n3   other  true\n", "")
    return subprocess.CompletedProcess(argv, 0, "", "")
  monkeypatch.setattr(bmc_module.subprocess, "run", fake_run)
  monkeypatch.setenv("IPMI_PASSWORD", "leaked-from-env")
  return calls


def test_ipmi_passwords_go_through_private_files_only(ipmitool, tmp_path):
  ipmi = Ipmi("10.9.0.5", str(tmp_path), 1, "lanplus", 5)
  ipmi.set_password(CUR, NEW["password"])
  ipmi.set_username({"username": "admin", "password": NEW["password"]}, "ops")
  for call in ipmitool:
    assert not any(s in " ".join(call["argv"]) for s in SECRETS)
    assert "IPMI_PASSWORD" not in call["env"]
    for content, mode in call["files"].values():
      assert mode == 0o600
  exec_lines = [c["files"]["exec"][0] for c in ipmitool if "exec" in c["files"]]
  assert exec_lines == [f"user set password 2 {NEW['password']}\n", "user set name 2 ops\n"]
  # and the files are gone
  assert os.listdir(tmp_path) == []


def test_ipmi_refused_login_is_false_not_unreachable(monkeypatch, tmp_path):
  monkeypatch.setattr(bmc_module.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
    argv, 1, "", "Error: Unable to establish IPMI v2 / RMCP+ session\nRAKP 2 message indicates an error : unauthorized name"))
  ipmi = Ipmi("10.9.0.5", str(tmp_path), 1, "lanplus", 5)
  ipmi.probe(CUR)
  assert ipmi.login(CUR) is False


def test_ipmi_that_doesnt_answer_is_unreachable(monkeypatch, tmp_path):
  monkeypatch.setattr(bmc_module.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
    argv, 1, "", "Error: Unable to establish IPMI v2 / RMCP+ session"))
  with pytest.raises(Unreachable):
    Ipmi("10.9.0.5", str(tmp_path), 1, "lanplus", 5).probe(CUR)


def test_a_long_ipmi_password_uses_the_20_byte_form(ipmitool, tmp_path):
  Ipmi("10.9.0.5", str(tmp_path), 1, "lanplus", 5).set_password(CUR, "a" * 18)
  assert [c["files"]["exec"][0] for c in ipmitool if "exec" in c["files"]] == [f"user set password 2 {'a' * 18} 20\n"]


# Redfish

def test_no_redfish_service_is_absent(monkeypatch):
  def refused(*a, **k):
    raise requests.exceptions.ConnectionError("Connection refused")
  monkeypatch.setattr(bmc_module.requests, "request", refused)
  with pytest.raises(Absent):
    Redfish("10.9.0.5", False, 5).probe()


def test_redfish_refusing_credentials_is_a_failed_login_not_ipmi(monkeypatch):
  def request(method, url, **kwargs):
    if url.endswith("/redfish/v1/"):
      return FakeResponse(200, {"AccountService": {"@odata.id": "/redfish/v1/AccountService"}})
    return FakeResponse(401, {})
  monkeypatch.setattr(bmc_module.requests, "request", request)
  redfish = Redfish("10.9.0.5", False, 5)
  redfish.probe()
  assert redfish.login(CUR) is False


def test_redfish_changes_the_account_with_its_etag(monkeypatch):
  seen = []

  def request(method, url, **kwargs):
    seen.append((method, url, kwargs.get("json"), kwargs.get("headers")))
    if url.endswith("/Accounts"):
      return FakeResponse(200, {"Members": [{"@odata.id": "/redfish/v1/AccountService/Accounts/1"},
                                            {"@odata.id": "/redfish/v1/AccountService/Accounts/2"}]})
    if url.endswith("/Accounts/1"):
      response = FakeResponse(200, {"UserName": "root"})
    elif url.endswith("/Accounts/2"):
      response = FakeResponse(200, {"UserName": "admin"})
    response.headers = {"ETag": 'W/"5"'}
    return response
  monkeypatch.setattr(bmc_module.requests, "request", request)
  Redfish("fd00::5", False, 5).set_password(CUR, NEW["password"])
  method, url, body, headers = seen[-1]
  assert (method, url) == ("PATCH", "https://[fd00::5]/redfish/v1/AccountService/Accounts/2")
  assert body == {"Password": NEW["password"]}
  assert headers == {"If-Match": 'W/"5"'}


def test_the_default_timeout_allows_a_slow_bmc():
  """ BMCs can take half a minute to apply an account change; at 15s the
  rig's slow BMC was reported unconfirmed although the change landed. """
  import yaml
  shipped = yaml.safe_load(open(os.path.join(os.path.dirname(bmc_module.__file__), "bmc-rotate.yaml")))
  assert bmc_rotate.timeout == shipped[0]["bmc-rotate"]["timeout"] == 60


# ipmitool's real output, from ipmi_sim (#343)
IPMI_WRONG_PASSWORD = ("Unable to Get Channel Cipher Suites\n> RAKP 2 HMAC is invalid\n"
                       "Error: Unable to establish IPMI v2 / RMCP+ session\n")
IPMI_NO_ANSWER = "Error: Unable to establish IPMI v2 / RMCP+ session\n"


def ipmitool_says(monkeypatch, out, rc=1):
  seen = []

  def run(argv, **kwargs):
    seen.append(argv)
    return subprocess.CompletedProcess(argv, rc, "", out)
  monkeypatch.setattr(bmc_module.subprocess, "run", run)
  return seen


def test_ipmitool_is_asked_why_a_login_failed(monkeypatch, tmp_path):
  """ Without -v, ipmitool prints the same line for a wrong password as for
  a BMC that doesn't answer (#343). """
  seen = ipmitool_says(monkeypatch, "", rc=0)
  Ipmi("10.9.0.5", str(tmp_path), 1, "lanplus", 5).login(CUR)
  assert seen[0][:2] == ["ipmitool", "-v"]


def test_a_wrong_ipmi_password_is_refused_not_unreachable(monkeypatch, tmp_path):
  ipmitool_says(monkeypatch, IPMI_WRONG_PASSWORD)
  ipmi = Ipmi("10.9.0.5", str(tmp_path), 1, "lanplus", 5)
  ipmi.probe(CUR)
  assert ipmi.login(CUR) is False


def test_an_ipmi_bmc_that_doesnt_answer_is_unreachable(monkeypatch, tmp_path):
  ipmitool_says(monkeypatch, IPMI_NO_ANSWER)
  with pytest.raises(Unreachable):
    Ipmi("10.9.0.5", str(tmp_path), 1, "lanplus", 5).probe(CUR)


def test_a_find_probes_with_a_candidate_not_the_cleared_current_pair(plugin, monkeypatch):
  """ The mPCC clears the current credentials when a change goes
  unconfirmed; probing IPMI with them made every find "unreachable" (#343). """
  probed = []

  def connect(address, workdir, pair):
    probed.append(pair)
    return FakeBMC([NEW])
  monkeypatch.setattr(plugin, "connect", connect)
  secret = {"kind": "find", "bmc": {"ipaddress": "10.9.0.5"}, "current": {"username": "", "password": ""},
            "candidates": [CUR, NEW]}
  result = plugin.rotate(secret, "/tmp", SECRETS)
  assert probed == [CUR]
  assert (result["status"], result["verified"]) == ("succeeded", "1")


def test_an_empty_ipmi_password_is_given_as_an_empty_argument(monkeypatch, tmp_path):
  seen = ipmitool_says(monkeypatch, "", rc=0)
  Ipmi("10.9.0.5", str(tmp_path), 1, "lanplus", 5).login({"username": "admin", "password": ""})
  assert seen[0][seen[0].index("-U"):seen[0].index("-U") + 4] == ["-U", "admin", "-P", ""]
  assert "-f" not in seen[0]


def test_a_password_file_problem_isnt_unreachable(monkeypatch, tmp_path):
  ipmitool_says(monkeypatch, "Unable to read password from file /tmp/x\n")
  with pytest.raises(RuntimeError, match="couldn't read the login password"):
    Ipmi("10.9.0.5", str(tmp_path), 1, "lanplus", 5).probe(CUR)
