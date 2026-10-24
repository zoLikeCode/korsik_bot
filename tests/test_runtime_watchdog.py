"""Host policy tests: no Docker daemon, systemd installation or model calls."""
import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/runtime-watchdog.py'
spec=importlib.util.spec_from_file_location('watchdog',SCRIPT)
w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)


def row(service='worker',health='unhealthy',status='running',started=100,project='korsik',identity='a'*64,oneoff='False'):
    return {'id':identity,'project':project,'service':service,'oneoff':oneoff,'status':status,'health':health,
            'started_at':datetime.fromtimestamp(started,timezone.utc).isoformat()}


class FakeDocker:
    project='korsik'
    services=('worker',)
    def __init__(self,rows):self.rows=rows;self.actions=[];self.calls=0
    def inspect(self):self.calls+=1;return copy.deepcopy(self.rows)
    def apply(self,action):self.actions.append(action)


@pytest.mark.parametrize('status,health',[('running','unhealthy'),('exited',None),('dead',None)])
def test_three_persisted_failures_trigger_only_exact_individual_container(tmp_path,status,health):
    docker=FakeDocker([row(status=status,health=health),row(project='other',identity='b'*64)])
    assert w.tick(docker,tmp_path,1000)['actions']==[]
    assert w.tick(docker,tmp_path,1020)['actions']==[]
    result=w.tick(docker,tmp_path,1040)
    assert result['actions']==[{'service':'worker','action':'restart','result':'REQUEST_ACCEPTED'}]
    assert docker.actions[0]['container_id']=='a'*64 and len(docker.actions)==1
    stored=json.loads((tmp_path/'watchdog-state.json').read_text())
    assert stored['services']['worker']['restart_timestamps']==[1040]
    assert (tmp_path/'watchdog-state.json').stat().st_mode&0o777==0o600


def test_startup_grace_and_model_load_grace_apply_even_after_three_samples():
    state={}
    for timestamp in [101,121,141,179]:
        assert w.plan(state,[row(started=0)],timestamp,services=('worker',))[0]==[]
    # A newly observed unknown timestamp waits from first observation.
    state={}
    for timestamp in [101,121,141,279]:
        assert w.plan(state,[row(started=100)],timestamp,services=('worker',))[0]==[]
    assert len(w.plan(state,[row(started=100)],280,services=('worker',))[0])==1
    state={}
    for timestamp in [101,121,141,399]:
        assert w.plan(state,[row('generator',started=100)],timestamp,services=('generator',))[0]==[]
    assert len(w.plan(state,[row('generator',started=100)],400,services=('generator',))[0])==1


def test_missing_service_is_created_only_after_grace_without_dependencies(tmp_path):
    docker=FakeDocker([])
    for timestamp in [1000,1020,1040,1179]:assert not w.tick(docker,tmp_path,timestamp)['actions']
    result=w.tick(docker,tmp_path,1180)
    assert result['actions'][0]['action']=='create' and docker.actions[0]['container_id'] is None


def test_cooldown_hourly_cap_and_clock_rollback_fail_closed():
    state={};rows=[row()]
    for timestamp in [1000,1020,1040]:actions,_=w.plan(state,rows,timestamp,services=('worker',))
    assert len(actions)==1
    for timestamp in [1060,1080,1100,1339]:assert w.plan(state,rows,timestamp,services=('worker',))[0]==[]
    assert len(w.plan(state,rows,1340,services=('worker',))[0])==1
    for timestamp in [1360,1380,1640,2000]:assert w.plan(state,rows,timestamp,services=('worker',))[0]==[]
    assert w.plan(state,rows,1999,services=('worker',))[1]['status']=='CLOCK_REGRESSION'
    assert len(w.plan(state,rows,4641,services=('worker',))[0])==1


def test_two_actions_per_tick_and_oneoff_or_unrelated_containers_never_restarted():
    services=('worker','api','scheduler');state={}
    rows=[row(s,identity=str(i+1)*64)for i,s in enumerate(services)]
    rows += [row('migrate',identity='9'*64),row('worker',project='other',identity='b'*64),row('api',identity='c'*64,oneoff='True')]
    for timestamp in [1000,1020,1040]:actions,_=w.plan(state,rows,timestamp,services=services)
    assert [a['service']for a in actions]==['worker','api'] and all(a['container_id']not in {'9'*64,'b'*64,'c'*64}for a in actions)


@pytest.mark.parametrize('marker',['maintenance','restore-in-progress'])
def test_maintenance_disables_even_inventory_inspection_and_any_changes(tmp_path,marker):
    docker=FakeDocker([row()]);(tmp_path/marker).touch()
    assert w.tick(docker,tmp_path,1000)=={'status':'MAINTENANCE','actions':[]}
    assert docker.calls==0 and docker.actions==[]


def test_fresh_inventory_recovery_or_restart_incarnation_skips_stale_action(tmp_path):
    docker=FakeDocker([row()])
    w.tick(docker,tmp_path,1000);w.tick(docker,tmp_path,1020)
    count=docker.calls
    def inspect():
        docker.calls+=1
        return [row()if docker.calls==count+1 else row(started=1030)]
    docker.inspect=inspect
    result=w.tick(docker,tmp_path,1040)
    assert result['actions'][0]['result']=='INVENTORY_CHANGED' and docker.actions==[]


def test_budget_reserved_before_failed_docker_change(tmp_path):
    docker=FakeDocker([row()])
    def failed(action):raise w.DockerError('fixed_safe_error')
    docker.apply=failed
    for timestamp in [1000,1020,1040]:result=w.tick(docker,tmp_path,timestamp)
    assert result['actions'][0]['result']=='DOCKER_ACTION_FAILED'
    assert json.loads((tmp_path/'watchdog-state.json').read_text())['services']['worker']['restart_timestamps']==[1040]
    assert w.tick(docker,tmp_path,1060)['actions']==[]


def test_corrupt_state_and_project_mismatch_do_not_change_docker(tmp_path):
    docker=FakeDocker([row()]);path=tmp_path/'watchdog-state.json'
    path.write_text('{unfinished')
    assert w.tick(docker,tmp_path,1000)['status']=='STATE_INVALID' and not docker.actions
    path.write_text(json.dumps({'schema_version':1,'services':{},'project':'other'}))
    assert w.tick(docker,tmp_path,1000)['status']=='STATE_PROJECT_MISMATCH' and not docker.actions


def test_exact_commands_do_not_pull_build_or_restart_the_whole_stack(tmp_path):
    docker=w.Docker(tmp_path,tmp_path/'.env','korsik');calls=[]
    docker.call=lambda args,timeout=20:calls.append(args)
    docker.apply({'action':'restart','service':'worker','container_id':'a'*64})
    assert calls[0]==['restart','--time','45','a'*64]
    docker.apply({'action':'create','service':'worker','container_id':None})
    assert calls[1][-7:]==['up','-d','--no-build','--no-deps','--pull','never','worker']
    assert 'down'not in str(calls) and 'prune'not in str(calls)


def test_installer_render_only_has_safe_boot_timer_and_bounded_journal(tmp_path):
    app=tmp_path/'app';(app/'scripts').mkdir(parents=True)
    (app/'compose.yaml').write_text('name: korsik\nservices: {}\n')
    (app/'.env').write_text('PRIVATE_TEST_MARKER=not-read-or-printed\n')
    (app/'scripts/runtime-watchdog.py').write_text(SCRIPT.read_text())
    target=tmp_path/'render'
    installer=SCRIPT.parent/'install-runtime-services.sh'
    result=subprocess.run(['bash',str(installer),'--app-dir',str(app),'--env-file',str(app/'.env'),'--state-dir',str(tmp_path/'runtime'),'--render-only',str(target)],text=True,capture_output=True)
    assert result.returncode==0,result.stderr
    boot=(target/'korsik-runtime.service').read_text();watch=(target/'korsik-watchdog.service').read_text();timer=(target/'korsik-watchdog.timer').read_text()
    assert 'Requires=docker.service'in boot and 'network-online.target'in boot and 'RemainAfterExit=yes'in boot
    assert 'up -d --no-build --pull never'in boot and 'ExecStop'not in boot
    assert 'OnUnitActiveSec=20s'in timer and 'Type=oneshot'in watch and 'restore-in-progress'in watch
    assert 'SystemMaxUse=64M'in (target/'journald/limits.conf').read_text()
    assert 'PRIVATE_TEST_MARKER'not in str(result) and not (tmp_path/'runtime').exists()


def test_docker_inventory_handles_missing_optional_health_for_oneshot_and_qdrant(tmp_path,monkeypatch):
    rows=[row('worker',health='healthy'),row('qdrant',health=None,identity='b'*64),row('migrate',health=None,status='exited',identity='c'*64)]
    def engine(command,**kwargs):
        if command[1]=='ps':return subprocess.CompletedProcess(command,0,'a'*12+'\n'+'b'*12+'\n'+'c'*12+'\n','')
        assert command[1]=='inspect'
        template=command[command.index('--format')+1]
        # Docker's Go formatter errors on .State.Health for an absent map key.
        # The actual all-16-container server proof separately evaluates this
        # template with the real engine, rather than a Python emulation.
        if '.State.Health'in template:
            return subprocess.CompletedProcess(command,1,'','missing optional health template key')
        assert 'index .State "Health"'in template
        return subprocess.CompletedProcess(command,0,'\n'.join(json.dumps(value)for value in rows),'')
    monkeypatch.setattr(w.subprocess,'run',engine)
    docker=w.Docker(tmp_path,tmp_path/'.env','korsik',services=('worker','qdrant'))
    observations=docker.inspect()
    assert len(observations)==3 and observations[1]['health']is None
    actions,summary=w.plan({},observations,1000,services=docker.services)
    assert not actions and all(item['status']=='HEALTHY'for item in summary['services'])


@pytest.mark.parametrize('bad_health',['unknown',{'Status':'healthy'},['healthy'],123])
def test_unknown_or_malformed_health_observation_fails_closed(tmp_path,monkeypatch,bad_health):
    def engine(command,**kwargs):
        if command[1]=='ps':return subprocess.CompletedProcess(command,0,'a'*12+'\n','')
        return subprocess.CompletedProcess(command,0,json.dumps(row(health=bad_health))+'\n','')
    monkeypatch.setattr(w.subprocess,'run',engine)
    result=w.tick(w.Docker(tmp_path,tmp_path/'.env','korsik',services=('worker',)),tmp_path,1000)
    assert result=={'status':'DOCKER_UNAVAILABLE','actions':[]}
