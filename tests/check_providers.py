"""Check the four Kubernetes provider stanzas with the pinned Zuul runtime (no API calls).

Run: python tests/check_providers.py --launcher /path/to/AID/deploy/zuul/server/launcher
Requires the full zuul==14.3.0 runtime and the coordinated AID companion.
Public globals/sections are expanded here solely to feed the upstream driver schema.
The production scheduler remains responsible for validating the complete layout.
"""
import argparse
import copy
import sys
from pathlib import Path
import yaml
from zuul.driver.kubernetes.kubernetesprovider import KubernetesProvider
parser = argparse.ArgumentParser()
parser.add_argument('--launcher', type=Path, required=True, help='AID companion deploy/zuul/server/launcher directory')
args = parser.parse_args()
sys.path.insert(0, str(args.launcher.resolve()))
from check_ssh import check_labels
from provider_identity import check_identity
from provider_pins import KUBERNETES, check_pins
ssh_labels = []
entries = yaml.safe_load((Path(__file__).resolve().parents[1] / 'zuul.d/providers.yaml').read_text())
check_pins(entries, args.launcher.resolve().parents[3])
check_identity(entries, args.launcher.resolve().parents[3])
by_kind = {kind: {entry[kind]['name']: entry[kind] for entry in entries if kind in entry} for kind in ['section', 'provider', 'image', 'label', 'flavor']}
for name, stanza in by_kind['provider'].items():
    if name == 'aid-thunderbox-staging': continue
    config = copy.deepcopy(by_kind['section'][stanza['section']])
    config.update(copy.deepcopy(stanza))
    for kind in ['image', 'label', 'flavor']:
        config[kind+'s'] = [{**copy.deepcopy(by_kind[kind][item['name']]), **item} for item in config[kind+'s']]
    ssh_labels.extend([{**item, 'project_canonical_name': 'github.com/kristofk/aid-zuul-config'} for item in config['labels']])
    result = KubernetesProvider.schema(config)
    for kind in ['image', 'label', 'flavor']:
        for item in result[kind+'s']:
            item['project_canonical_name'] = 'github.com/kristofk/aid-zuul-config'
    print(name, 'schema OK', result['connection'], result['resource_limits'])
    assert result['connection'] == KUBERNETES[name]['connection'], result['connection']
    assert result['resource_limits'] == {'pods': KUBERNETES[name]['pods']}
    assert len(result['labels']) == len(result['images']) == 1
    label, image = result['labels'][0], result['images'][0]
    assert (label['name'], image['name']) == (KUBERNETES[name]['label'], KUBERNETES[name]['image'])
    assert not label['reuse'] and label['slots'] == 1
    assert image['connection_type'] == 'ssh' and image['connection_port'] == 2222
print('All four bounded Kubernetes providers pass the Zuul 14.3 schema on their pinned connections; three check pods, one gate, one gate compute and one post, one slot per pod, no reuse, SSH on 2222.')

check_labels(ssh_labels)
print('All four real provider labels scanned SSH host keys; executor known_hosts populated without disabling strict checking.')
