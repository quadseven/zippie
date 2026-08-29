.PHONY: test smoke install-agent lint sync-home-pkg

test:
	cd travel/bond-agent && python3 -m pytest -q

smoke:
	./scripts/smoke-test.sh

install-agent:
	cd travel/bond-agent && pip3 install -e .

lint:
	cd travel/bond-agent && python3 -m ruff check zippie tests || true

# The home transport runs the zippie PACKAGE, shipped into the pod as a
# ConfigMap. kustomize refuses a symlink outside its root, so a copy is forced
# and test_manifest_copy_in_sync.py is what stops it drifting. This target is
# how you satisfy that guard after editing any shipped module - the module list
# is read from the guard itself, so the two cannot disagree.
sync-home-pkg:
	@python3 -c "import re,pathlib,shutil; \
	g=pathlib.Path('travel/bond-agent/tests/test_manifest_copy_in_sync.py').read_text(); \
	mods=re.findall(r'\"([a-z_]+\.py)\"', g[g.index('PKG_MODULES'):g.index(']', g.index('PKG_MODULES'))]); \
	[shutil.copyfile('travel/bond-agent/zippie/'+m, 'deploy/oke/zippie-home/zippie-pkg/'+m) for m in mods]; \
	print('synced: '+' '.join(mods))"
