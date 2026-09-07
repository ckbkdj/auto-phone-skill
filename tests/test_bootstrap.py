"""Regression cases reproduced from the user's startup log, with synthetic private data."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from auto_phone.contracts import Fault, OUTPUT, dumps, validate
from auto_phone.environment import architecture, capture, configure, inspect, prepare, runtime_env, setup_status
from auto_phone.jdk import select_asset, trusted_url, unpack
from auto_phone.platform import Config
from auto_phone.runtime import Runtime
from test_core import FakePhone, screen

ROOT = Path(__file__).resolve().parents[1]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / 'state'
        self.home.mkdir()
        self.env = patch.dict(os.environ, {'AUTO_PHONE_HOME': str(self.home), 'AUTO_PHONE_CONFIG': '',
                             'AUTO_PHONE_UDID': '', 'AUTO_PHONE_DEVICE_ID': '', 'JAVA_HOME': '',
                             'ANDROID_HOME': '', 'ANDROID_SDK_ROOT': ''})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def cfg(self):
        (self.home / 'config.json').write_text(dumps({'devices': {'cloud': {'udid': 'assigned-device'}}}))
        return Config()

    def test_explicit_missing_configuration_does_not_silently_use_another_file(self):
        self.cfg()
        with self.assertRaises(Fault) as e:
            Config(Path(self.tmp.name) / 'typo.json')
        self.assertEqual(e.exception.code, 'CONFIG_NOT_FOUND')

    def test_skill_local_config_compatibility_does_not_depend_on_current_directory(self):
        module = Path(self.tmp.name) / 'download-name/auto_phone/platform.py'
        module.parent.mkdir(parents=True)
        local = module.parent.parent / 'config.json'
        local.write_text(dumps({'devices': {'cloud': {'udid': 'host-assigned'}}}))
        with patch('auto_phone.platform.__file__', str(module)):
            c = Config()
            self.assertEqual(c.source, 'skill_local_compat')
            self.assertEqual(c.path, local)
            self.assertEqual(c.device('cloud')['udid'], 'host-assigned')
            private = self.cfg()
            self.assertEqual(private.source, 'private')
            self.assertTrue(private.local_config_ignored)

    def test_setup_validates_and_writes_exact_private_path_and_backs_up(self):
        c = configure(Config(), {'device_id': 'cloud', 'udid': 'assigned'})
        self.assertEqual(c.path, self.home / 'config.json')
        self.assertEqual(c.device('cloud')['udid'], 'assigned')
        changed = configure(c, {'device_id': 'cloud', 'java_home': str(self.home / 'jdk')})
        self.assertTrue(list((self.home / 'config-backups').glob('*.json')))
        self.assertEqual(changed.raw['java_home'], str(self.home / 'jdk'))
        if os.name != 'nt':
            self.assertEqual(c.path.stat().st_mode & 0o777, 0o600)

    def test_setup_replacement_requires_explicit_operator_flag(self):
        c = self.cfg()
        with self.assertRaises(Fault):
            configure(c, {'device_id': 'cloud', 'udid': 'other'})
        self.assertEqual(Config().device('cloud')['udid'], 'assigned-device')

    def test_invalid_setup_does_not_clobber_file(self):
        c = self.cfg()
        before = c.path.read_bytes()
        for body in [{'device_id':'cloud','system_port':True}, {'device_id':'cloud','run_shell':'x'},
                     {'device_id':'cloud','appium_url':'http://0.0.0.0:4723','replace_device':True}]:
            with self.subTest(body=body), self.assertRaises(Fault):
                configure(c, body)
            self.assertEqual(c.path.read_bytes(), before)

    def test_environment_supplied_device_can_be_registered_without_guessing(self):
        with patch.dict(os.environ, {'AUTO_PHONE_DEVICE_ID':'cloud','AUTO_PHONE_UDID':'assigned-by-host'}):
            c = configure(Config(), {'device_id':'cloud'})
            self.assertEqual(c.device('cloud')['udid'], 'assigned-by-host')

    def test_jre_is_not_a_jdk_and_sdk_is_not_just_adb_on_path(self):
        c = self.cfg()
        jre = self.home / 'jre'; (jre / 'bin').mkdir(parents=True)
        (jre / 'bin/java').touch()
        c.raw['java_home'] = str(jre)
        c.raw['android_home'] = str(self.home / 'missing-sdk')
        with patch('auto_phone.environment.shutil.which', return_value=None):
            r = inspect(c)
        self.assertFalse(r['jdk_ready'])
        self.assertFalse(r['sdk_ready'])
        self.assertIn('JDK_REQUIRED', r['issues'])
        self.assertIn('ANDROID_SDK_REQUIRED', r['issues'])

    def test_private_java_sdk_environment_survives_new_cli_process(self):
        c = self.cfg()
        jdk = self.home / 'toolchains/jdk'
        sdk = self.home / 'sdk'
        for file in [jdk/'bin/javac', jdk/'bin/java', jdk/'bin/keytool', sdk/'platform-tools/adb']:
            file.parent.mkdir(parents=True, exist_ok=True); file.touch()
        configure(c, {'device_id':'cloud', 'java_home':str(jdk), 'android_home':str(sdk)})
        env = runtime_env(Config())
        self.assertEqual(env['JAVA_HOME'], str(jdk))
        self.assertEqual(env['ANDROID_HOME'], str(sdk))
        self.assertIn(str(jdk/'bin'), env['PATH'])
        self.assertIn(str(sdk/'platform-tools'), env['PATH'])

    def test_missing_prerequisites_stop_before_npm_install_and_task_creation(self):
        c = self.cfg()
        with patch('auto_phone.platform.Http.call', side_effect=Fault('TRANSPORT_UNAVAILABLE')), \
             patch('auto_phone.environment.inspect', return_value={'issues':['JDK_REQUIRED','ANDROID_SDK_REQUIRED']}), \
             patch('auto_phone.platform.subprocess.run') as execute:
            with self.assertRaises(Fault) as e:
                prepare(c, 'cloud')
            self.assertEqual(e.exception.code,'JDK_REQUIRED')
        execute.assert_not_called()
        self.assertEqual(setup_status(c)['code'], 'JDK_REQUIRED')
        self.assertFalse((self.home/'state.sqlite3').exists())

    def test_existing_remote_appium_does_not_require_local_java_or_sdk(self):
        c = self.cfg()
        c.devices['cloud']['appium_url'] = 'https://appium.example'
        with patch('auto_phone.platform.Http.call', return_value={'value':{'ready':True}}), \
             patch('auto_phone.environment.require_local', side_effect=AssertionError('should not check host toolchain')):
            prepare(c, 'cloud')
        self.assertEqual(setup_status(c)['stage'], 'appium_ready')

    def test_permission_error_is_not_retried_using_different_tool(self):
        with patch('auto_phone.environment.subprocess.run', side_effect=PermissionError) as run:
            with self.assertRaises(Fault) as e:
                capture(['java','-version'], dict(os.environ))
        self.assertEqual(e.exception.code, 'HOST_EXEC_POLICY_DENIED')
        self.assertEqual(run.call_count, 1)

    def test_same_idempotency_key_retries_only_effect_free_initialization_failure(self):
        c = self.cfg(); phone = FakePhone()
        attempts = []
        def factory(*_):
            attempts.append(1)
            if len(attempts) == 1: raise Fault('JDK_REQUIRED')
            return phone
        runtime = Runtime(c, factory)
        try:
            request = {'goal':'test','device_id':'cloud','idempotency_key':'turn'}
            failed = runtime.call('begin',request)
            self.assertEqual(failed['status'],'initialization_failed')
            self.assertEqual(failed['code'],'JDK_REQUIRED')
            succeeded = runtime.call('begin',request)
            self.assertTrue(succeeded['ok'], succeeded)
            self.assertEqual(succeeded['task_id'],failed['task_id'])
            self.assertEqual(phone.calls,[])
        finally:
            runtime.close()

    def test_new_turn_not_blocked_by_failed_initialization(self):
        c = self.cfg()
        runtime = Runtime(c, lambda *_: (_ for _ in ()).throw(Fault('JDK_REQUIRED')))
        try:
            first = runtime.call('begin',{'goal':'test','device_id':'cloud','idempotency_key':'old'})
            second = runtime.call('begin',{'goal':'test','device_id':'cloud','idempotency_key':'new'})
            self.assertEqual(second['code'],'JDK_REQUIRED')
            self.assertNotEqual(first['task_id'],second['task_id'])
        finally: runtime.close()

    def test_old_failed_turn_cannot_resume_over_a_new_active_owner(self):
        c = self.cfg(); phone = FakePhone()
        runtime = Runtime(c, lambda *_: (_ for _ in ()).throw(Fault('JDK_REQUIRED')))
        try:
            old = {'goal':'test','device_id':'cloud','idempotency_key':'old'}
            runtime.call('begin', old)
            runtime.phone_factory = lambda *_: phone
            active = runtime.call('begin', {**old, 'idempotency_key':'new'})
            self.assertTrue(active['ok'])
            self.assertEqual(runtime.call('begin', old)['code'], 'DEVICE_HAS_ACTIVE_TASK')
            self.assertEqual(phone.calls, [])
        finally:
            runtime.close()

    def test_legacy_crash_or_boot_failure_without_actions_is_reclaimed(self):
        c = self.cfg(); runtime = Runtime(c,lambda *_:FakePhone())
        try:
            old, _ = runtime.store.new({'goal':'test','device_id':'cloud','idempotency_key':'old'},c.identity('cloud'))
            self.assertEqual(old['status'],'active')
            started = runtime.call('begin',{'goal':'test','device_id':'cloud','idempotency_key':'new'})
            self.assertTrue(started['ok'],started)
            self.assertEqual(runtime.store.get(old['id'])['status'],'initialization_failed')
        finally: runtime.close()

    def test_never_release_initialized_or_started_or_unknown_task(self):
        for variant in ('observed','started','unknown'):
            with self.subTest(variant=variant):
                # A new private store for each case, with no real device side effects.
                folder = self.home/variant; folder.mkdir()
                c = self.cfg(); c.home = folder
                runtime=Runtime(c,lambda *_:FakePhone())
                try:
                    old, _ = runtime.store.new({'goal':'test','device_id':'cloud','idempotency_key':'old'},c.identity('cloud'))
                    if variant=='observed': old['observation']=screen('Ready')
                    elif variant=='started': runtime.store.start(old,'op','digest')
                    else: old['status']='outcome_unknown'
                    with runtime.store.db: runtime.store.save(old)
                    response=runtime.call('begin',{'goal':'test','device_id':'cloud','idempotency_key':'new'})
                    self.assertEqual(response['code'],'DEVICE_HAS_ACTIVE_TASK')
                finally: runtime.close()

    def test_generic_init_error_releases_only_effect_free_task(self):
        c=self.cfg();runtime=Runtime(c,lambda *_:(_ for _ in ()).throw(ValueError('private credentials never output')))
        try:
            out=runtime.call('begin',{'goal':'test','device_id':'cloud','idempotency_key':'a'})
            self.assertEqual(out['status'],'initialization_failed')
            self.assertNotIn('credentials',dumps(out))
        finally:runtime.close()

    def test_task_diagnostics_and_status_do_not_dump_ui_secrets_or_tokens(self):
        c=self.cfg();phone=FakePhone();phone.current=screen('Private current page')
        r=Runtime(c,lambda *_:phone)
        try:
            task=r.call('begin',{'goal':'private goal','device_id':'cloud','idempotency_key':'a'})
            listing=r.call('tasks',{})
            self.assertNotIn('private',dumps(listing).lower())
            status=r.call('status',{'task_id':task['task_id']})
            self.assertNotIn('observation',status)
            validate(OUTPUT,listing)
        finally:r.close()

    def test_upgrade_keeps_old_skill_outside_scan_root(self):
        from auto_phone.entry import install
        dest=Path(self.tmp.name)/'skills'
        target=install(ROOT,dest)
        (target/'old-marker').write_text('backup me')
        install(ROOT,dest,upgrade=True)
        backups=list((self.home/'skill-backups').glob('*/old-marker'))
        self.assertEqual(len(backups),1)
        self.assertFalse((target/'old-marker').exists())


class JdkArchive(unittest.TestCase):
    def metadata(self,arch='aarch64',image='jdk'):
        return json.dumps([{'binary':{'architecture':arch,'os':'linux','image_type':image,'jvm_impl':'hotspot',
            'package':{'checksum':'a'*64,'size':100,'link':'https://github.com/adoptium/temurin17-binaries/releases/download/test/jdk.tar.gz'}}}]).encode()

    def test_arm64_mapping_and_metadata_do_not_select_x64_or_jre(self):
        self.assertEqual(architecture('arm64'),'aarch64')
        self.assertEqual(architecture('aarch64'),'aarch64')
        for raw in (self.metadata('x64'),self.metadata(image='jre')):
            with self.assertRaises(Fault): select_asset(raw,'aarch64','linux')
        self.assertEqual(select_asset(self.metadata(),'aarch64','linux')[1],'a'*64)

    def test_checksum_and_official_download_origin_required(self):
        raw=json.loads(self.metadata());raw[0]['binary']['package']['checksum']='bad'
        with self.assertRaises(Fault):select_asset(json.dumps(raw).encode(),'aarch64','linux')
        for u in ['http://github.com/a','https://evil.example/jdk','https://user:pass@github.com/a']:
            with self.assertRaises(Fault):trusted_url(u)

    def test_archive_path_escape_and_unsafe_links_are_rejected(self):
        for name,target in [('../escape',None),('/escape',None),('jdk/link','../../escape')]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp);a=p/'a.tar'
                with tarfile.open(a,'w') as tar:
                    info=tarfile.TarInfo(name)
                    if target:
                        info.type=tarfile.SYMTYPE;info.linkname=target
                    else:info.size=1
                    tar.addfile(info,None if target else io.BytesIO(b'x'))
                with self.assertRaises(Fault):unpack(a,p/'extracted')

    def test_regular_jdk_archive_preserves_executable_bits(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);a=p/'a.tar'
            with tarfile.open(a,'w') as tar:
                info=tarfile.TarInfo('jdk/bin/java');info.size=2;info.mode=0o755
                tar.addfile(info,io.BytesIO(b'xx'))
            unpack(a,p/'extracted')
            self.assertEqual((p/'extracted/jdk/bin/java').read_bytes(),b'xx')



class JdkPipeline(unittest.TestCase):
    setUp = Fixture.setUp
    tearDown = Fixture.tearDown
    cfg = Fixture.cfg
    def asset(self):
        p=Path(self.tmp.name)/'fixture.tar.gz'
        with tarfile.open(p,'w:gz') as tar:
            for n in ('java','javac','keytool'):
                data=b'verified fixture only'
                info=tarfile.TarInfo('jdk-test/bin/'+n);info.mode=0o755;info.size=len(data)
                tar.addfile(info,io.BytesIO(data))
        return p.read_bytes()

    def test_arm_jdk_checksum_extract_and_restart_without_second_download(self):
        from auto_phone.jdk import install_jdk
        blob=self.asset();c=self.cfg();urls=[]
        metadata=json.loads(JdkArchive().metadata())
        metadata[0]['binary']['package'].update(checksum=hashlib.sha256(blob).hexdigest(),size=len(blob))
        def get(url,path,*args):
            urls.append(url)
            path.write_bytes(json.dumps(metadata).encode() if 'assets/latest' in url else blob)
        def run(args,*_):
            return 'os.arch = aarch64\n' if '-XshowSettings:properties' in args else 'javac 17.0.16'
        with patch('auto_phone.jdk.platform.machine',return_value='aarch64'), \
             patch('auto_phone.jdk.platform.system',return_value='Linux'), \
             patch('auto_phone.jdk.platform.libc_ver',return_value=('glibc','2.36')), \
             patch('auto_phone.jdk.download',side_effect=get),patch('auto_phone.jdk.capture',side_effect=run):
            install_jdk(c)
            install_jdk(c)
        self.assertEqual(len(urls),2)
        self.assertIn('architecture=aarch64',urls[0]);self.assertIn('image_type=jdk',urls[0])
        self.assertTrue((c.home/'toolchains/jdk/bin/javac').is_file())

    def test_wrong_checksum_never_extracts_or_executes_binary(self):
        from auto_phone.jdk import install_jdk
        blob=self.asset();c=self.cfg();metadata=json.loads(JdkArchive().metadata())
        metadata[0]['binary']['package']['size']=len(blob)
        def get(url,path,*_):path.write_bytes(json.dumps(metadata).encode() if 'assets/latest' in url else blob)
        with patch('auto_phone.jdk.platform.machine',return_value='aarch64'), \
             patch('auto_phone.jdk.platform.system',return_value='Linux'), \
             patch('auto_phone.jdk.platform.libc_ver',return_value=('glibc','2.36')), \
             patch('auto_phone.jdk.download',side_effect=get),patch('auto_phone.jdk.capture') as execute:
            with self.assertRaises(Fault) as e:install_jdk(c)
        self.assertEqual(e.exception.code,'JDK_CHECKSUM_MISMATCH');execute.assert_not_called()
        self.assertFalse((c.home/'toolchains/jdk').exists())

class LegacyInstallation(unittest.TestCase):
    setUp = Fixture.setUp
    tearDown = Fixture.tearDown
    def test_upgrade_migrates_valid_local_config_and_archives_legacy_scan_entry(self):
        from auto_phone.entry import install
        destination=Path(self.tmp.name)/'skills';legacy=destination/'auto-phone-skill-main'
        legacy.mkdir(parents=True)
        (legacy/'SKILL.md').write_text('---\nname: auto-phone-skill\n---\n')
        (legacy/'auto_phone').mkdir();(legacy/'auto_phone/runtime.py').write_text('pass')
        (legacy/'config.json').write_text(dumps({'devices':{'cloud':{'udid':'assigned-device'}}}))
        target=install(ROOT,destination,upgrade=True)
        self.assertTrue(target.is_dir());self.assertFalse(legacy.exists())
        self.assertEqual(Config().device('cloud')['udid'],'assigned-device')
        self.assertTrue(list((self.home/'skill-backups').glob('legacy-*/config.json')))


if __name__ == '__main__':
    unittest.main()
