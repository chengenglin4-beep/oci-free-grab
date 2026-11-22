"""One fail-closed OCI launch attempt. No credentials or instance details are logged."""
import base64
import json
import os
import sys
import time
import random
import urllib.error
import urllib.request
import uuid

import oci

REGION = 'ap-singapore-1'
AD_SUFFIX = 'AD-1'
SHAPE = 'VM.Standard.A1.Flex'
STATE_PATH = 'state.json'
GH_URL = 'https://api.github.com/repos/' + os.environ.get('GITHUB_REPOSITORY', '') + '/contents/' + STATE_PATH


def gh_request(method, payload=None):
    req = urllib.request.Request(GH_URL, method=method, headers={
        'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'],
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Type': 'application/json',
    }, data=None if payload is None else json.dumps(payload).encode())
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def get_state():
    response = gh_request('GET')
    return json.loads(base64.b64decode(response['content'])), response['sha']


def put_state(state, sha):
    body = base64.b64encode(json.dumps(state, sort_keys=True).encode()).decode()
    result = gh_request('PUT', {'message': 'Update launch safety state', 'content': body, 'sha': sha})
    return result['content']['sha']


def all_results(call, *args, **kwargs):
    return oci.pagination.list_call_get_all_results(call, *args, **kwargs).data


def main():
    state, sha = get_state()
    if state.get('status') == 'cooldown':
        if time.time() < state.get('until', float('inf')):
            print('429 cooldown in effect; waiting for the next schedule.', flush=True)
            return 0
        put_state({'status': 'idle'}, sha)
        state, sha = get_state()
    if state.get('status') == 'created':
        print('Instance was already created. No further launch.')
        return 0
    if state.get('status') in ('pending', 'authentication_failed'):
        print('Launch paused: safety state is ' + state['status'] + '; investigate before resuming.', flush=True)
        return 0
    if state != {'status': 'idle'}:
        print('Unknown safety state; no launch will be submitted.', flush=True)
        return 1
    settings = json.loads(os.environ.get('OCI_SETTINGS', '{}'))
    def setting(name):
        return settings[name] if name in settings else os.environ[name]
    config = {
        'tenancy': setting('OCI_TENANCY'), 'user': setting('OCI_USER'),
        'fingerprint': setting('OCI_FINGERPRINT'), 'key_content': os.environ['OCI_API_KEY'],
        'region': REGION,
    }
    tenancy = config['tenancy']
    identity = oci.identity.IdentityClient(config, timeout=(10, 30))
    compute = oci.core.ComputeClient(config, timeout=(10, 30))
    block = oci.core.BlockstorageClient(config, timeout=(10, 30))
    network = oci.core.VirtualNetworkClient(config, timeout=(10, 30))
    try:
        ads = all_results(identity.list_availability_domains, tenancy)
    except oci.exceptions.ServiceError as error:
        if error.status == 401 and os.environ.get('DRY_RUN', '1') == '0':
            put_state({'status': 'authentication_failed'}, sha)
            print('Authentication failed (401). Automatic launches stopped.')
            return 1
        raise
    matches = [x.name for x in ads if x.name.endswith(AD_SUFFIX)]
    if len(matches) != 1:
        raise RuntimeError('Cannot uniquely identify AD-1')
    ad = matches[0]
    comps = all_results(identity.list_compartments, tenancy, compartment_id_in_subtree=True, access_level='ACCESSIBLE')
    if any(x.lifecycle_state != 'DELETED' for x in comps):
        raise RuntimeError('Additional compartments require a full inventory')
    instances = all_results(compute.list_instances, tenancy)
    active = [x for x in instances if x.lifecycle_state not in ('TERMINATED', 'TERMINATING')]
    if any(x.display_name == 'sg-node-01' for x in active):
        print('Target instance already exists. No creation.')
        return 0
    if any(x.shape == SHAPE for x in active):
        raise RuntimeError('Existing A1 instance consumes the free allowance')
    boots = all_results(block.list_boot_volumes, availability_domain=ad, compartment_id=tenancy)
    volumes = all_results(block.list_volumes, compartment_id=tenancy)
    total = sum(x.size_in_gbs for x in boots + volumes if x.lifecycle_state != 'TERMINATED')
    if total + 50 > 200:
        raise RuntimeError('Storage allowance would be exceeded')
    subnet = network.get_subnet(setting('OCI_SUBNET')).data
    if subnet.lifecycle_state != 'AVAILABLE' or subnet.prohibit_public_ip_on_vnic:
        raise RuntimeError('Subnet is unavailable or disallows public IPs')
    images = all_results(compute.list_images, tenancy, shape=SHAPE,
                         operating_system='Canonical Ubuntu', operating_system_version='24.04 Minimal aarch64',
                         sort_by='TIMECREATED', sort_order='DESC')
    images = [x for x in images if 'Minimal' in x.display_name and 'aarch64' in x.display_name and x.lifecycle_state == 'AVAILABLE']
    if not images:
        raise RuntimeError('Ubuntu 24.04 Minimal ARM image unavailable')
    if os.environ.get('DRY_RUN', '1') != '0':
        print('Dry run: AD-1; A1 Flex; 2 OCPU; 12 GB; default boot volume; public IPv4.')
        return 0
    token = str(uuid.uuid4())
    pending_sha = put_state({'status': 'pending', 'token': token}, sha)
    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=ad, compartment_id=tenancy, display_name='sg-node-01', shape=SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=2, memory_in_gbs=12),
        source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=images[0].id),
        create_vnic_details=oci.core.models.CreateVnicDetails(subnet_id=subnet.id, assign_public_ip=True),
        metadata={'ssh_authorized_keys': setting('SSH_PUBLIC_KEY')},
    )
    try:
        compute.launch_instance(details, opc_retry_token=token, retry_strategy=oci.retry.NoneRetryStrategy())
    except oci.exceptions.ServiceError as error:
        msg = str(error.message or '').lower()
        if error.status == 401:
            put_state({'status': 'authentication_failed'}, pending_sha)
            print('401 authentication failed. Automatic launches stopped.', flush=True)
            return 1
        if error.status == 429:
            put_state({'status': 'cooldown', 'until': time.time() + 300}, pending_sha)
            print('429 rate limited. Backing off for at least 300 seconds.', flush=True)
            return 0
        if 'out of host capacity' in msg:
            put_state({'status': 'idle'}, pending_sha)
            print('Out of host capacity.', flush=True)
            return 10
        print('OCI rejected the launch or its outcome is uncertain. Safety lock remains pending.')
        return 1
    put_state({'status': 'created'}, pending_sha)
    print('Launch accepted. Inspect the OCI console for status and public IP.')
    return 0


def run_loop():
    deadline = time.monotonic() + 300
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        print(f'Attempt {attempt} started at {time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}', flush=True)
        try:
            result = main()
        except oci.exceptions.ServiceError as error:
            if error.status in (401, 429):
                state, sha = get_state()
                if state == {'status': 'idle'}:
                    replacement = {'status': 'authentication_failed'} if error.status == 401 else {'status': 'cooldown', 'until': time.time() + 300}
                    put_state(replacement, sha)
                print('401 authentication failed.' if error.status == 401 else '429 rate limited. Backing off for at least 300 seconds.', flush=True)
                return 1 if error.status == 401 else 0
            raise
        if result != 10:
            return result
        delay = 120 + random.randint(10, 30)
        if time.monotonic() + delay >= deadline:
            print('Run time limit reached; next schedule will retry.', flush=True)
            return 0
        print(f'Waiting {delay} seconds before next capacity retry.', flush=True)
        time.sleep(delay)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(run_loop())
    except Exception as exc:
        # Exception text may contain account identifiers or credential paths.
        print('Safety stop:', type(exc).__name__)
        sys.exit(1)

