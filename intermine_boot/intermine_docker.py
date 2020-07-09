import docker
from pathlib import Path
import pickle as pkl
import subprocess
import shutil
import os
from git import Repo,cmd
import yaml
from intermine_boot import utils


DOCKER_COMPOSE_REPO = 'https://github.com/intermine/docker-intermine-gradle'

ENV_VARS = ['env', 'UID='+str(os.geteuid()), 'GID='+str(os.getegid())]

# all docker containers created would be attached to this network
DOCKER_NETWORK_NAME = 'intermine_boot'

def _get_docker_user():
    return str(os.getuid()) + ':' + str(os.getgid())

def _is_conf_same(path_to_config, options):
    conf_file_path = str(path_to_config) + '/.config'
    if not os.path.isfile(conf_file_path):
        return False
    
    config = pkl.load(open(conf_file_path, 'rb'))
    try:
        if (config['branch_name'] == options['im_branch']) and (
                config['repo_name'] == options['im_repo']):
            return True
        else:
            return False
    except KeyError:
        return False


def _store_conf(path_to_config, options):
    config = {}
    config['branch_name'] = options['im_branch']
    config['repo_name'] = options['im_repo']

    f = open(path_to_config / '.config', 'wb')
    pkl.dump(config, f)
    return


def _get_compose_path(options, env):
    #work_dir = env['data_dir'] / 'docker'
    work_dir = Path(__file__).parent.parent / 'docker-intermine-gradle'
    compose_file = 'dockerhub.docker-compose.yml'
    if options['build_images']:
        compose_file = 'local.docker-compose.yml'
    return work_dir / compose_file

def _create_volumes(env):
    data_dir = env['data_dir'] / 'docker' / 'data'
    os.mkdir(data_dir)
    os.mkdir(data_dir / 'solr')
    os.mkdir(data_dir / 'postgres')
    os.mkdir(data_dir / 'mine')
    os.mkdir(data_dir / 'mine' / 'dumps')
    os.mkdir(data_dir / 'mine' / 'configs')
    os.mkdir(data_dir / 'mine' / 'packages')
    os.mkdir(data_dir / 'mine' / 'intermine')
    os.mkdir(data_dir / 'mine' / 'biotestmine')
    os.mkdir(data_dir / 'mine' / '.intermine')
    os.mkdir(data_dir / 'mine' / '.m2')

def up(options, env):
    compose_path = _get_compose_path(options, env)

    same_conf_exist = False
    if (env['data_dir'] / 'docker').is_dir():
        if _is_conf_same(env['data_dir'], options):
            print ('Same configuration exist. Running local compose file...') 
            same_conf_exist = True
        else:
            print ('Configuration change detected. Downloading compose file...')
            shutil.rmtree(env['data_dir'])
    
    if not same_conf_exist:
        (env['data_dir'] / 'docker/').mkdir(parents=True, exist_ok=True)

    _create_volumes(env)

    option_vars = (['IM_REPO_URL='+options['im_repo'],
                    'IM_REPO_BRANCH='+options['im_branch']]
                   if options['build_im'] else [])

    client = docker.from_env()
    if options['build_images']:
        print ('Building images...')
        img_path = compose_path.parent
        tomcat_image = client.images.build(
            path=str(img_path / 'tomcat'), tag='tomcat', dockerfile='tomcat.Dockerfile')[0]
        solr_image = client.images.build(
            path=str(img_path / 'solr'), tag='solr', dockerfile='solr.Dockerfile')[0]
        postgres_image = client.images.build(
            path=str(img_path / 'postgres'), tag='postgres', dockerfile='postgres.Dockerfile')[0]
        intermine_builder_image = client.images.build(
            path=str(img_path / 'intermine_builder'), tag='builder', dockerfile='intermine_builder.Dockerfile')[0]
    else:
        print ('Pulling images...')
        tomcat_image = client.images.pull('intermine/tomcat:latest')
        solr_image = client.images.pull('intermine/solr:latest')
        postgres_image = client.images.pull('intermine/postgres:latest')
        intermine_builder_image = client.images.pull('intermine/builder:latest')

    docker_network = client.networks.create(DOCKER_NETWORK_NAME)
    print ('Starting containers...')
    tomcat = create_tomcat_container(client, tomcat_image)
    solr = create_solr_container(client, solr_image, env)
    postgres = create_postgres_container(client, postgres_image, env)
    intermine_builder = create_intermine_builder_container(
        client, intermine_builder_image, env)
    
    _store_conf(env['data_dir'], options)


def remove_container(client, container_name):
    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        container = None
    
    if container is not None:
        container.remove(force=True)


def down(options, env):
    client = docker.from_env()
    remove_container(client, 'tomcat_container')
    remove_container(client, 'postgres_container')
    remove_container(client, 'solr_container')
    remove_container(client, 'intermine_container')


def create_archives(options, env):
    compose_path = _get_compose_path(options, env)

    postgres_archive = env['data_dir'] / 'postgres'
    postgres_data_dir = compose_path.parent / 'data' / 'postgres'
    shutil.make_archive(postgres_archive, 'zip', root_dir=postgres_data_dir)

    solr_archive = env['data_dir'] / 'solr'
    solr_data_dir = compose_path.parent / 'data' / 'solr'
    shutil.make_archive(solr_archive, 'zip', root_dir=solr_data_dir)

    mine_archive = env['data_dir'] / 'biotestmine'
    mine_data_dir = compose_path.parent / 'data' / 'mine' / 'biotestmine'
    shutil.make_archive(mine_archive, 'zip', root_dir=mine_data_dir)


def create_tomcat_container(client, image):
    envs = {
        'MEM_OPTS': os.environ.get('MEM_OPTS', '-Xmx1g -Xms500m')
    }

    ports = {
        8080: 9999
    }

    print ('\n\nStarting Tomcat container...\n')
    tomcat_container = client.containers.run(
        image, name='tomcat', environment=envs, ports=ports,
        detach=True, network=DOCKER_NETWORK_NAME)

    for log in tomcat_container.logs(stream=True, timestamps=True):
        print(log)
        if 'Server startup' in str(log):
            break
    
    return tomcat_container


def create_solr_container(client, image, env):
    envs = {
        'MEM_OPTS': os.environ.get('MEM_OPTS', '-Xmx2g -Xms1g'),
        'MINE_NAME': os.environ.get('MINE_NAME', 'biotestmine')
    }

    user = _get_docker_user()

    data_dir = env['data_dir'] / 'docker' / 'data' / 'solr'
    volumes = {
        data_dir: {
            'bind': '/var/solr',
            'mode': 'rw'
        }
    }

    print('\n\nStarting Solr container...\n')
    solr_container = client.containers.run(
        image, name='solr', environment=envs, user=user, volumes=volumes,
        detach=True, network=DOCKER_NETWORK_NAME)

    for log in solr_container.logs(stream=True, timestamps=True):
        print (log)
        if 'Registered new searcher' in str(log):
            break

    return solr_container


def create_postgres_container(client, image, env):
    user = _get_docker_user()
    data_dir = env['data_dir'] / 'docker' / 'data' / 'postgres'
    volumes = {
        data_dir : {
            'bind': '/var/lib/postgresql/data',
            'mode': 'rw'
        }
    }

    print ('\n\nStarting Postgres container...\n')
    postgres_container = client.containers.run(
        image, name='postgres', user=user, volumes=volumes,
        detach=True, network=DOCKER_NETWORK_NAME)

    for log in postgres_container.logs(stream=True, timestamps=True):
        print (log)
        if 'autovacuum launcher started' in str(log):
            break

    return postgres_container


def create_intermine_builder_container(client, image, env):
    user = _get_docker_user()

    data_dir = env['data_dir'] / 'docker' / 'data'

    # IM_DATA_DIR temporarily removed
    environment = {
        'MINE_NAME': os.environ.get('MINE_NAME', 'biotestmine'),
        'MINE_REPO_URL': os.environ.get('MINE_REPO_URL', ''),
        'MEM_OPTS': os.environ.get('MEM_OPTS', '-Xmx2g -Xms1g'),
        'IM_DATA_DIR': os.environ.get('IM_DATA_DIR', ''),
        'IM_REPO_URL': os.environ.get('IM_REPO_URL', ''),
        'IM_REPO_BRANCH': os.environ.get('IM_REPO_BRANCH', '')
    }

    mine_path = env['data_dir'] / 'docker' / 'data' / 'mine'

    volumes = {
        mine_path / 'dump': {
            'bind': '/home/intermine/intermine/dump',
            'mode': 'rw'
        },

        mine_path / 'configs': {
            'bind': '/home/intermine/intermine/configs',
            'mode': 'rw'
        },
        mine_path / 'packages': {
            'bind': '/home/intermine/.m2',
            'mode': 'rw'
        },
        mine_path / 'intermine': {
            'bind': '/home/intermine/.intermine',
            'mode': 'rw'
        },
        mine_path / 'biotestmine': {
            'bind': '/home/intermine/intermine/biotestmine',
            'mode': 'rw'
        }
    }

    print ('\n\nStarting Intermine container...\n\n')

    try:
        assert client.containers.get('postgres').status == 'running'
    except AssertionError:
        print ('Postgres container not running. Exiting...')
        exit(1)

    try:
        assert client.containers.get('tomcat').status == 'running'
    except AssertionError:
        print ('Tomcat container not running. Exiting...')
        exit(1)

    try:
        assert client.containers.get('solr').status == 'running'
    except AssertionError:
        print ('Solr container not running. Exiting...')

    try:
        intermine_builder_container = client.containers.run(
            image, name='intermine_builder', user=user, environment=environment,
            volumes=volumes, detach=False, stream=True, network=DOCKER_NETWORK_NAME)

    except docker.errors.ImageNotFound:
        print ('docker image not found. Exiting...')
        exit(1)
    except docker.errors.ContainerError as e:
        print ('Error while running container')
        print (e.msg)
        exit(1)

    return intermine_builder_container