"""Read-only OCI inventory, without printing credentials or OCIDs."""
import json
import os
import sys
import oci

try:
    settings = json.loads(os.environ.get('OCI_SETTINGS', '{}'))
    def setting(name):
        return settings[name] if name in settings else os.environ[name]
    config = {'tenancy': setting('OCI_TENANCY'), 'user': setting('OCI_USER'),
              'fingerprint': setting('OCI_FINGERPRINT'), 'key_content': os.environ['OCI_API_KEY'],
              'region': 'ap-singapore-1'}
    identity = oci.identity.IdentityClient(config, timeout=(10, 30))
    compute = oci.core.ComputeClient(config, timeout=(10, 30))
    block = oci.core.BlockstorageClient(config, timeout=(10, 30))
    get_all = oci.pagination.list_call_get_all_results
    ads = get_all(identity.list_availability_domains, config['tenancy']).data
    ad = [x.name for x in ads if x.name.endswith('AD-1')]
    print('Authentication: OK; AD-1 matches:', len(ad), flush=True)
    comps = get_all(identity.list_compartments, config['tenancy'], compartment_id_in_subtree=True,
                    access_level='ACCESSIBLE').data
    active_comps = [x for x in comps if x.lifecycle_state != 'DELETED']
    print('Additional compartments:', len(active_comps), flush=True)
    for comp in [config['tenancy']] + [x.id for x in active_comps]:
        instances = get_all(compute.list_instances, comp).data
        for inst in instances:
            print('Instance:', inst.display_name, 'status:', inst.lifecycle_state, 'shape:', inst.shape, flush=True)
        print('Active instances in compartment:', sum(x.lifecycle_state not in ('TERMINATED', 'TERMINATING') for x in instances), flush=True)
        if len(ad) == 1:
            boots = get_all(block.list_boot_volumes, availability_domain=ad[0], compartment_id=comp).data
            vols = get_all(block.list_volumes, compartment_id=comp).data
            print('Active storage GiB:', sum(x.size_in_gbs for x in boots + vols if x.lifecycle_state != 'TERMINATED'), flush=True)
except Exception as exc:
    print('Read-only inventory failed:', type(exc).__name__, 'status:', getattr(exc, 'status', 'unknown'), flush=True)
    sys.exit(1)
