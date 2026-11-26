#!/usr/bin/env python3
"""Build stable and versioned manifests from published immutable releases."""
import hashlib, json, os, pathlib, re, subprocess, urllib.error, urllib.request

def semver_key(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', version)
    if not match:
        raise ValueError('version must be canonical stable SemVer')
    return tuple(map(int, match.groups()))


def highest_manifest(manifests):
    if not manifests:
        raise ValueError('at least one release manifest is required')
    return max(manifests, key=lambda item: semver_key('.'.join(map(str, item[0]))))


def validate_stable_transition(old_version, new_version, old_raw, new_raw):
    if old_version > new_version:
        raise ValueError('Refusing to downgrade Pages stable manifest')
    if old_version == new_version and old_raw != new_raw:
        raise ValueError('Current Pages stable manifest differs for the same release version')


def main():
    repo=os.environ['GITHUB_REPOSITORY']; token=os.environ['GH_TOKEN']
    base='https://api.github.com/repos/'+repo
    headers={'Accept':'application/vnd.github+json','Authorization':'Bearer '+token,
             'X-GitHub-Api-Version':'2026-03-10','User-Agent':'miura-panel-pages-manifest'}
    releases=[]; url=base+'/releases?per_page=100'
    while url:
        req=urllib.request.Request(url,headers=headers)
        with urllib.request.urlopen(req,timeout=30) as response:
            chunk=json.loads(response.read(4*1024*1024).decode())
            link=response.headers.get('Link','')
        releases.extend(chunk)
        url=None
        for item in link.split(','):
            if 'rel="next"' in item:
                url=item[item.find('<')+1:item.find('>')]
                break
    semver=re.compile(r'^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$')
    candidates=[]
    for release in releases:
        match=semver.fullmatch(release.get('tag_name',''))
        if release.get('draft') or release.get('prerelease') or not match: continue
        asset=next((a for a in release.get('assets',[]) if a.get('name')=='miura-panel-update-manifest.json'),None)
        if asset:
            if release.get('immutable') is not True:
                raise SystemExit('Refusing to distribute a manifest from a mutable release: '+release['tag_name'])
            candidates.append((tuple(map(int,match.groups())),release,asset))
    if not candidates: raise SystemExit('No published release contains a compatibility manifest')
    out=pathlib.Path(os.environ['RUNNER_TEMP'])/'miura-pages'/'updates'
    out.mkdir(parents=True,exist_ok=True)
    manifests=[]; existing_versioned=set()
    for version_tuple,release,asset in sorted(candidates):
        by_name={item.get('name'):item for item in release.get('assets',[])}
        installer_asset=by_name.get(f"install-spanish-empire-panel-v{'.'.join(map(str,version_tuple))}.sh")
        if not installer_asset: raise SystemExit('Release is missing installer asset for '+release['tag_name'])
        version='.'.join(map(str,version_tuple))
        if installer_asset.get('size',0)>2*1024*1024:
            raise SystemExit('Installer asset exceeds the v1.0.34 compatibility limit: '+release['tag_name'])
        req=urllib.request.Request(asset['browser_download_url'],headers={'User-Agent':'miura-panel-pages-manifest'})
        with urllib.request.urlopen(req,timeout=30) as response: raw=response.read(65537)
        if len(raw)>65536: raise SystemExit('Release manifest exceeds 64 KiB: '+release['tag_name'])
        info=json.loads(raw.decode('utf-8'))
        if info.get('version')!=version or info.get('installer')!=installer_asset.get('browser_download_url') or not re.fullmatch(r'[0-9a-f]{64}',str(info.get('sha256',''))):
            raise SystemExit('Invalid version or SHA256 in manifest for '+release['tag_name'])
        manifest_digest=hashlib.sha256(raw).hexdigest()
        if asset.get('digest') and asset['digest']!='sha256:'+manifest_digest:
            raise SystemExit('Manifest asset digest mismatch for '+release['tag_name'])
        installer_api_digest=installer_asset.get('digest')
        if installer_api_digest:
            if installer_api_digest!='sha256:'+info['sha256']:
                raise SystemExit('Manifest installer SHA256 differs from release asset digest for '+release['tag_name'])
        else:
            req=urllib.request.Request(installer_asset['browser_download_url'],headers={'User-Agent':'miura-panel-pages-manifest'})
            with urllib.request.urlopen(req,timeout=30) as response: installer_bytes=response.read(2*1024*1024+1)
            if len(installer_bytes)>2*1024*1024 or hashlib.sha256(installer_bytes).hexdigest()!=info['sha256']:
                raise SystemExit('Downloaded installer size/SHA256 mismatch for '+release['tag_name'])
        dest=out/f'v{version}.json'
        version_url='https://juanramonvf.github.io/miura-panel/updates/'+dest.name
        try:
            req=urllib.request.Request(version_url,headers={'User-Agent':'miura-panel-pages-manifest'})
            with urllib.request.urlopen(req,timeout=20) as response: existing_version=response.read(65537)
            if len(existing_version)>65536 or existing_version!=raw:
                raise SystemExit('Refusing to replace a versioned immutable manifest: '+dest.name)
            existing_versioned.add(version)
        except urllib.error.HTTPError as exc:
            if exc.code!=404: raise SystemExit(f'Cannot safely read existing {dest.name} (HTTP {exc.code})')
        except SystemExit:
            raise
        except Exception as exc:
            raise SystemExit('Cannot safely read existing '+dest.name+': '+str(exc))
        dest.write_bytes(raw)
        digest=hashlib.sha256(raw).hexdigest()
        (out/(dest.name+'.sha256')).write_text(f'{digest}  {dest.name}\n',encoding='ascii')
        manifests.append((version_tuple,raw))
    latest=highest_manifest(manifests)
    stable_url='https://juanramonvf.github.io/miura-panel/updates/stable.json'
    try:
        req=urllib.request.Request(stable_url,headers={'User-Agent':'miura-panel-pages-manifest'})
        with urllib.request.urlopen(req,timeout=20) as response: old_raw=response.read(65537)
        if len(old_raw)>65536: raise SystemExit('Current Pages stable manifest exceeds 64 KiB')
        old=json.loads(old_raw.decode('utf-8'))
    except urllib.error.HTTPError as exc:
        if exc.code!=404:
            raise SystemExit(f'Cannot safely read current Pages stable manifest (HTTP {exc.code})')
        if existing_versioned:
            raise SystemExit('Pages stable manifest is missing even though versioned manifests exist')
        old_raw=None; old=None
    except Exception as exc:
        raise SystemExit('Cannot safely read current Pages stable manifest: '+str(exc))
    if old is not None:
        old_match=re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)',str(old.get('version','')))
        if not old_match: raise SystemExit('Current Pages stable manifest has invalid version')
        old_version=tuple(map(int,old_match.groups()))
        try:
            validate_stable_transition(old_version, latest[0], old_raw, latest[1])
        except ValueError as exc:
            raise SystemExit(str(exc))
    stable=out/'stable.json'; stable.write_bytes(latest[1])
    stable_digest=hashlib.sha256(stable.read_bytes()).hexdigest()
    (out/'stable.json.sha256').write_text(f'{stable_digest}  stable.json\n',encoding='ascii')
    (out.parent/'index.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><title>Miura Panel update feeds</title><h1>Miura Panel update feeds</h1><p>Stable update manifest: <a href="updates/stable.json">updates/stable.json</a>.</p><p>Versioned manifests are immutable snapshots for each published GitHub Release.</p></html>\n',encoding='utf-8')
    for checksum_file in out.rglob('*.sha256'):
        subprocess.run(['sha256sum', '-c', checksum_file.name], cwd=checksum_file.parent, check=True)
    print('Prepared Pages stable.json at highest published semver', '.'.join(map(str,latest[0])))


if __name__ == '__main__':
    main()
