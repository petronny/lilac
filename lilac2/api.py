from __future__ import annotations

import logging
import shutil
import re
import os
import subprocess
import traceback
from typing import Dict, List, Union
from typing import Tuple, Optional, Iterable, Iterator
import fileinput
import tempfile
from pathlib import Path
from types import SimpleNamespace
import tarfile
import io
from collections.abc import Container

import requests

from myutils import at_dir
from htmlutils import parse_document_from_requests

from .cmd import run_cmd, git_pull, git_push, git_reset_hard
from . import const
from .const import _G, SPECIAL_FILES
from .typing import PkgRel
from .pypi2pkgbuild import gen_pkgbuild
from .pkgbuild import format_package_version as _format_package_version

git_push

logger = logging.getLogger(__name__)

_g = SimpleNamespace()
UserAgent = 'lilac/0.2b (package auto-build bot, by lilydjwg)'

s = requests.Session()
s.headers['User-Agent'] = UserAgent

VCS_SUFFIXES = ('-git', '-hg', '-svn', '-bzr')
AUR_URL = 'https://aur.archlinux.org/rpc/'

def _unquote_item(s: str) -> Optional[str]:
  m = re.search(r'''[ \t'"]*([^ '"]+)[ \t'"]*''', s)
  if m is not None:
    return m.group(1)
  else:
    return None

def _add_into_array(line: str, values: Iterable[str]) -> str:
  l = line.find('(')
  r = line.rfind(')')
  if r != -1:
    line_l, line_m, line_r = line[:l+1], line[l+1:r], line[r:]
  else:
    line_l, line_m, line_r = line[:l+1], line[l+1:], ''
  arr = {_unquote_item(x) for x in line_m.split(' ')}.union(values)
  arr_nonone = [i for i in arr if i is not None]
  arr_nonone.sort()
  arr_elems_str = "'{}'".format("' '".join(arr_nonone))
  line = line_l + arr_elems_str + line_r
  return line

def add_into_array(which: str, extra_deps: Iterable[str]) -> None:
  '''
  Add more values into the shell array
  '''
  field_appeared = False

  pattern = re.compile(r'\s*' + re.escape(which) + r'=')
  for line in edit_file('PKGBUILD'):
    if pattern.match(line):
      line = _add_into_array(line, extra_deps)
      field_appeared = True
    print(line)

  if not field_appeared:
    with open('PKGBUILD', 'a') as f:
      line = f'{which}=()'
      line = _add_into_array(line, extra_deps)
      f.write(line + '\n')

def add_arch(extra_arches: Iterable[str]) -> None:
  add_into_array('arch', extra_arches)

def add_depends(extra_deps: Iterable[str]) -> None:
  add_into_array('depends', extra_deps)

def add_makedepends(extra_deps: Iterable[str]) -> None:
  add_into_array('makedepends', extra_deps)

def add_checkdepends(extra_deps: Iterable[str]) -> None:
  add_into_array('checkdepends', extra_deps)

def add_conflicts(extra_conflicts: Iterable[str]) -> None:
  add_into_array('conflicts', extra_conflicts)

def add_replaces(extra_replaces: Iterable[str]) -> None:
  add_into_array('replaces', extra_replaces)

def add_provides(extra_provides: Iterable[str]) -> None:
  add_into_array('provides', extra_provides)

def add_groups(extra_groups: Iterable[str]) -> None:
  add_into_array('groups', extra_groups)

def edit_file(filename: str) -> Iterator[str]:
  with fileinput.input(files=(filename,), inplace=True) as f:
    for line in f:
      yield line.rstrip('\n')

def obtain_array(name: str) -> Optional[List[str]]:
  '''
  Obtain an array variable from PKGBUILD.
  Works by calling bash to source PKGBUILD, writing the array to a temporary file, and reading from the file.
  '''
  with tempfile.NamedTemporaryFile() as output_file:
    command_write_array_out = """printf "%s\\0" "${{{}[@]}}" > {}""" \
        .format(name, output_file.name)
    command_export_array = ['bash', '-c', "source PKGBUILD && {}".format(
      command_write_array_out)]
    subprocess.run(command_export_array, stderr=subprocess.PIPE,
                   check=True)
    res = output_file.read().decode()
    if res == '\0':
      return None
    variable = res.split('\0')[:-1]
    return variable

def obtain_depends() -> Optional[List[str]]:
  return obtain_array('depends')

def obtain_makedepends() -> Optional[List[str]]:
  return obtain_array('makedepends')

def obtain_optdepends(
  parse_dict: bool=True
) -> Optional[Union[Dict[str, str], List[str]]]:
  obtained_array = obtain_array('optdepends')
  if not obtained_array:
    return obtained_array
  if parse_dict:
    return {pkg.strip(): desc.strip() for (pkg, desc) in
            (item.split(':', 1) for item in obtained_array)}
  else:
    return obtained_array

def vcs_update() -> None:
  # clean up the old source tree
  shutil.rmtree('src', ignore_errors=True)
  run_cmd(['makepkg', '-od', '--noprepare', '-A'], use_pty=True)

def get_pkgver_and_pkgrel(
) -> Tuple[Optional[str], Optional[PkgRel]]:
  pkgrel = None
  pkgver = None
  try:
    with open('PKGBUILD') as f:
      for l in f:
        if l.startswith('pkgrel='):
          pkgrel = l.rstrip().split(
            '=', 1)[-1].strip('\'"')
          try:
            pkgrel = int(pkgrel) # type: ignore
          except (ValueError, TypeError):
            pass
        elif l.startswith('pkgver='):
          pkgver = l.rstrip().split('=', 1)[-1]
  except FileNotFoundError:
    pass

  return pkgver, pkgrel

def _next_pkgrel(rel: PkgRel) -> int:
  if isinstance(rel, int):
    return rel + 1

  first_segment = rel.split('.')[0]
  return int(first_segment) + 1

def update_pkgver_and_pkgrel(
  newver: str, *, updpkgsums: bool = True) -> None:

  pkgver, pkgrel = get_pkgver_and_pkgrel()
  assert pkgver is not None and pkgrel is not None

  for line in edit_file('PKGBUILD'):
    if line.startswith('pkgver=') and pkgver != newver:
        line = f'pkgver={newver}'
    elif line.startswith('pkgrel='):
      if pkgver != newver:
        line = 'pkgrel=1'
      else:
        line = f'pkgrel={_next_pkgrel(pkgrel)}'

    print(line)

  if updpkgsums:
    run_cmd(["updpkgsums"])

def update_pkgrel(
  rel: Optional[PkgRel] = None,
) -> None:
  with open('PKGBUILD') as f:
    pkgbuild = f.read()

  def replacer(m):
    nonlocal rel
    if rel is None:
      rel = _next_pkgrel(m.group(1))
    return str(rel)

  pkgbuild = re.sub(r'''(?<=^pkgrel=)['"]?([\d.]+)['"]?''', replacer, pkgbuild, count=1, flags=re.MULTILINE)
  with open('PKGBUILD', 'w') as f:
    f.write(pkgbuild)
  logger.info('pkgrel updated to %s', rel)

def pypi_pre_build(
  depends: Optional[List[str]] = None,
  python2: bool = False,
  pypi_name: Optional[str] = None,
  arch: Optional[Iterable[str]] = None,
  makedepends: Optional[List[str]] = None,
  depends_setuptools: bool = True,
  provides: Optional[Iterable[str]] = None,
  conflicts: Optional[Iterable[str]] = None,
  prepare: Optional[str] = None,
  check: Optional[str] = None,
  optdepends: Optional[List[str]] = None,
  license: Optional[str] = None,
  license_file: Optional[str] = None,
) -> None:
  if python2:
    raise ValueError('pypi_pre_build no longer supports python2')

  pkgname = os.path.basename(os.getcwd())
  if pypi_name is None:
    pypi_name = pkgname.split('-', 1)[-1]

  _new_pkgver, pkgbuild = gen_pkgbuild(
    pypi_name,
    pkgname = pkgname,
    depends = depends,
    arch = arch,
    makedepends = makedepends,
    depends_setuptools = depends_setuptools,
    provides = provides,
    conflicts = conflicts,
    prepare = prepare,
    check = check,
    optdepends = optdepends,
    license = license,
    license_file = license_file,
  )

  with open('PKGBUILD', 'w') as f:
    f.write(pkgbuild)

def pypi_post_build() -> None:
  git_add_files('PKGBUILD')
  git_commit()

def git_add_files(
  files: Union[str, List[str]], *, force: bool = False,
) -> None:
  if isinstance(files, str):
    files = [files]
  try:
    if force:
      run_cmd(['git', 'add', '-f', '--'] + files)
    else:
      run_cmd(['git', 'add', '--'] + files)
  except subprocess.CalledProcessError:
    # on error, there may be a partial add, e.g. some files are ignored
    run_cmd(['git', 'reset', '--'] + files)
    raise

def git_commit(*, check_status: bool = True) -> None:
  if check_status:
    ret = [x for x in
           run_cmd(["git", "status", "-s", "."]).splitlines()
           if x.split(None, 1)[0] != '??']
    if not ret:
      return

  pkgbase = os.path.split(os.getcwd())[1]
  built_version = _format_package_version(
    _G.epoch, _G.pkgver, _G.pkgrel)
  msg = f'{pkgbase}: auto updated to {built_version}'
  run_cmd(['git', 'commit', '-m', msg])

class AurDownloadError(Exception):
  def __init__(self, pkgname: str) -> None:
    self.pkgname = pkgname

def _allow_update_aur_repo(pkgname: str, diff: str) -> bool:
  is_vcs = pkgname.endswith(VCS_SUFFIXES)
  for line in diff.splitlines():
    if not line.startswith(('+', '-')) or line.startswith(('+++', '---')):
      # Not a changed line
      continue
    line = line[1:]  # remove the +/- marker
    if is_vcs and not line.startswith(('pkgver=', 'pkgrel=')):
      return True
    if not is_vcs and not line.startswith('pkgrel='):
      return True
  return False

def _update_aur_repo_real(pkgname: str) -> None:
  aurpath = const.AUR_REPO_DIR / pkgname
  if not aurpath.is_dir():
    logger.info('cloning AUR repo: %s', aurpath)
    with at_dir(const.AUR_REPO_DIR):
      run_cmd(['git', 'clone', f'aur@aur.archlinux.org:{pkgname}.git'])
  else:
    with at_dir(aurpath):
      git_reset_hard()
      git_pull()

  with at_dir(aurpath):
    logger.info('removing old files from AUR repo: %s', aurpath)
    oldfiles = run_cmd(['git', 'ls-files']).splitlines()
    for f in oldfiles:
      logger.debug('removing file %s', f)
      try:
        os.unlink(f)
      except OSError as e:
        logger.warning('failed to remove file %s: %s', f, e)

  logger.info('copying files to AUR repo: %s', aurpath)
  files = run_cmd(['git', 'ls-files']).splitlines()
  for f in files:
    if f in const.SPECIAL_FILES:
      continue
    logger.debug('copying file %s', f)
    shutil.copy(f, aurpath)

  with at_dir(aurpath):
    if not _allow_update_aur_repo(pkgname, run_cmd(['git', 'diff'])):
      return

    with open('.SRCINFO', 'wb') as srcinfo:
      subprocess.run(
        ['makepkg', '--printsrcinfo'],
        stdout = srcinfo,
        check = True,
      )
    run_cmd(['git', 'add', '.'])
    p = subprocess.run(['git', 'diff-index', '--quiet', 'HEAD'])
    if p.returncode != 0:
      built_version = _format_package_version(
        _G.epoch, _G.pkgver, _G.pkgrel)
      msg = f'[lilac] updated to {built_version}'
      run_cmd(['git', 'commit', '-m', msg])
      run_cmd(['git', 'push'])

def update_aur_repo() -> None:
  pkgbase = _G.mod.pkgbase
  try:
    _update_aur_repo_real(pkgbase)
  except subprocess.CalledProcessError as e:
    tb = traceback.format_exc()
    _G.repo.send_error_report(
      _G.mod,
      exc = (e, tb),
      subject = '提交软件包 %s 到 AUR 时出错',
    )

def git_pkgbuild_commit() -> None:
  git_add_files('PKGBUILD')
  git_commit()

def _prepend_self_path() -> None:
  mydir = Path(__file__).resolve().parent.parent
  path = os.environ['PATH']
  os.environ['PATH'] = str(mydir / path)

def single_main(build_prefix: str = None, build_args=None, makechrootpkg_args=None, makepkg_args=None) -> None:
  from nicelogger import enable_pretty_logging
  from . import lilacpy
  from .building import lilac_build

  _prepend_self_path()
  enable_pretty_logging('DEBUG')
  with lilacpy.load_lilac(Path('.')) as mod:
    if build_prefix is None:
      build_prefix = mod.build_prefix
    if build_args:
      mod.build_args = build_args
    if makechrootpkg_args:
      mod.makechrootpkg_args = makechrootpkg_args
    if makepkg_args:
      mod.makepkg_args = makepkg_args
    lilac_build(
      mod, None,
      build_prefix = build_prefix,
      accept_noupdate = True,
    )

def clean_directory() -> List[str]:
  '''clean all PKGBUILD and related files'''
  files = run_cmd(['git', 'ls-files']).splitlines()
  logger.info('clean directory')
  ret = []
  for f in files:
    if f in SPECIAL_FILES:
      continue
    logger.debug('unlink file %s', f)
    ret.append(f)
    try:
      os.unlink(f)
    except FileNotFoundError:
      pass
  return ret

def _try_aur_url(name: str) -> bytes:
  aur4url = 'https://aur.archlinux.org/cgit/aur.git/snapshot/{name}.tar.gz'
  templates = [aur4url]
  urls = [url.format(first_two=name[:2], name=name) for url in templates]
  for url in urls:
    response = s.get(url)
    if response.status_code == 200:
      logger.debug("downloaded aur tarball '%s' from url '%s'", name, url)
      return response.content
  logger.error("failed to find aur url for '%s'", name)
  raise AurDownloadError(name)

def _download_aur_pkgbuild(name: str) -> List[str]:
  content = io.BytesIO(_try_aur_url(name))
  files = []
  with tarfile.open(
    name=name+".tar.gz", mode="r:gz", fileobj=content
  ) as tarf:
    for tarinfo in tarf:
      basename, remain = os.path.split(tarinfo.name)
      if basename == '':
        continue
      if remain in ('.AURINFO', '.SRCINFO', '.gitignore'):
        continue
      tarinfo.name = remain
      tarf.extract(tarinfo)
      files.append(remain)
  return files

def git_rm_files(files: List[str]) -> None:
  if files:
    run_cmd(['git', 'rm', '--cached', '--'] + files)

def aur_pre_build(
  name: Optional[str] = None, *, do_vcs_update: Optional[bool] = None,
  maintainers: Union[str, Container[str]] = (),
) -> None:
  # import pyalpm here so that lilac can be easily used on non-Arch
  # systems (e.g. Travis CI)
  import pyalpm

  if name is None:
    name = os.path.basename(os.getcwd())
  if maintainers:
    info = s.get(AUR_URL, params={"v": 5, "type": "info", "arg[]": name}).json()
    maintainer = info['results'][0]['Maintainer']
    error = False
    if isinstance(maintainers, str):
      error = maintainer != maintainers
    else:
      error = maintainer not in maintainers
    if error:
      raise Exception('unexpected AUR package maintainer', maintainer)

  pkgver, pkgrel = get_pkgver_and_pkgrel()
  _g.aur_pre_files = clean_directory()
  _g.aur_building_files = _download_aur_pkgbuild(name)

  aur_pkgver, aur_pkgrel = get_pkgver_and_pkgrel()
  if pkgver and pkgver == aur_pkgver:
    if pyalpm.vercmp(f'1-{pkgrel}', f'1-{aur_pkgrel}') < 0:
      # use aur pkgrel
      pass
    else:
      # bump
      update_pkgrel()

  if do_vcs_update is None:
    do_vcs_update = name.endswith(VCS_SUFFIXES)

  if do_vcs_update:
    vcs_update()
    # recheck after sync, because AUR pkgver may lag behind
    new_pkgver, new_pkgrel = get_pkgver_and_pkgrel()
    if pkgver and pkgver == new_pkgver:
      if pkgrel is None:
        next_pkgrel = 1
      else:
        next_pkgrel = _next_pkgrel(pkgrel)
      if pyalpm.vercmp(f'1-{next_pkgrel}', f'1-{new_pkgrel}') > 0:
        update_pkgrel(next_pkgrel)

def aur_post_build() -> None:
  git_rm_files(_g.aur_pre_files)
  git_add_files(_g.aur_building_files, force=True)
  output = run_cmd(["git", "status", "-s", "."]).strip()
  if output:
    git_commit()
  del _g.aur_pre_files, _g.aur_building_files

def action_post_build() -> None:
  check_srcinfo()
  output = run_cmd(["git", "status", "-s", "."]).strip()
  if output:
    git_commit()

def download_official_pkgbuild(name: str) -> List[str]:
  url = 'https://www.archlinux.org/packages/search/json/?name=' + name
  logger.info('download PKGBUILD for %s.', name)
  info = s.get(url).json()
  r = [r for r in info['results'] if r['repo'] != 'testing'][0]
  repo = r['repo']
  arch = r['arch']
  if repo in ('core', 'extra'):
    gitrepo = 'packages'
  else:
    gitrepo = 'community'
  pkgbase = [r['pkgbase'] for r in info['results'] if r['repo'] != 'testing'][0]

  tree_url = 'https://projects.archlinux.org/svntogit/%s.git/tree/repos/%s-%s?h=packages/%s' % (
    gitrepo, repo, arch, pkgbase)
  doc = parse_document_from_requests(tree_url, s)
  blobs = doc.xpath('//div[@class="content"]//td/a[contains(concat(" ", normalize-space(@class), " "), " ls-blob ")]')
  files = [x.text for x in blobs]
  for filename in files:
    blob_url = 'https://projects.archlinux.org/svntogit/%s.git/plain/repos/%s-%s/%s?h=packages/%s' % (
      gitrepo, repo, arch, filename, pkgbase)
    with open(filename, 'wb') as f:
      logger.debug('download file %s.', filename)
      data = s.get(blob_url).content
      f.write(data)
  return files

