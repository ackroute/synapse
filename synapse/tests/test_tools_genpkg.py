import os

import synapse.exc as s_exc
import synapse.common as s_common
import synapse.tests.utils as s_test
import synapse.tests.files as s_files

import synapse.tools.genpkg as s_genpkg

dirname = os.path.dirname(__file__)

class GenPkgTest(s_test.SynTest):

    async def test_tools_genpkg(self):

        with self.raises(s_exc.NoSuchFile):
            ymlpath = s_common.genpath(dirname, 'files', 'stormpkg', 'nosuchfile.yaml')
            await s_genpkg.main((ymlpath,))

        with self.raises(s_exc.BadPkgDef):
            ymlpath = s_common.genpath(dirname, 'files', 'stormpkg', 'nopath.yaml')
            await s_genpkg.main((ymlpath,))

        with self.raises(s_exc.BadPkgDef):
            ymlpath = s_common.genpath(dirname, 'files', 'stormpkg', 'nomime.yaml')
            await s_genpkg.main((ymlpath,))

        with self.raises(s_exc.BadPkgDef):
            ymlpath = s_common.genpath(dirname, 'files', 'stormpkg', 'notitle.yaml')
            await s_genpkg.main((ymlpath,))

        with self.raises(s_exc.BadPkgDef):
            ymlpath = s_common.genpath(dirname, 'files', 'stormpkg', 'nocontent.yaml')
            await s_genpkg.main((ymlpath,))

        ymlpath = s_common.genpath(dirname, 'files', 'stormpkg', 'testpkg.yaml')
        async with self.getTestCore() as core:

            savepath = s_common.genpath(core.dirn, 'testpkg.json')

            url = core.getLocalUrl()
            argv = ('--push', url, '--save', savepath, ymlpath)

            await s_genpkg.main(argv)

            msgs = await core.stormlist('testpkgcmd')
            self.stormIsInPrint('argument <foo> is required', msgs)
            msgs = await core.stormlist('$mod=$lib.import(testmod) $lib.print($mod)')
            self.stormIsInPrint('Imported Module testmod', msgs)

            pdef = s_common.yamlload(savepath)

            self.eq(pdef['name'], 'testpkg')
            self.eq(pdef['version'], (0, 0, 1))
            self.eq(pdef['modules'][0]['name'], 'testmod')
            self.eq(pdef['modules'][0]['storm'], 'inet:ipv4\n')
            self.eq(pdef['modules'][1]['name'], 'testpkg.testext')
            self.eq(pdef['modules'][1]['storm'], 'inet:fqdn\n')
            self.eq(pdef['modules'][2]['name'], 'testpkg.testextfile')
            self.eq(pdef['modules'][2]['storm'], 'inet:fqdn\n')
            self.eq(pdef['commands'][0]['name'], 'testpkgcmd')
            self.eq(pdef['commands'][0]['storm'], 'inet:ipv6\n')

            self.eq(pdef['optic']['files']['index.html']['file'], 'aGkK')

            self.eq(pdef['docs'][0]['title'], 'Foo Bar')
            self.eq(pdef['docs'][0]['content'], 'Hello!\n')

            self.eq(pdef['logo']['mime'], 'image/svg')
            self.eq(pdef['logo']['file'], 'c3R1ZmYK')

            # nodocs
            savepath = s_common.genpath(core.dirn, 'testpkg_nodocs.json')
            argv = ('--no-docs', '--save', savepath, ymlpath)

            await s_genpkg.main(argv)

            pdef = s_common.yamlload(savepath)

            self.eq(pdef['name'], 'testpkg')
            self.eq(pdef['docs'][0]['title'], 'Foo Bar')
            self.eq(pdef['docs'][0]['content'], '')

            # No push, no save:  nothing to do
            argv = (ymlpath,)
            retn = await s_genpkg.main(argv)
            self.ne(0, retn)

            # Invalid:  save with pre-made file
            argv = ('--no-build', '--save', savepath, savepath)
            retn = await s_genpkg.main(argv)
            self.ne(0, retn)

            # Push a premade json
            argv = ('--no-build', '--push', url, savepath)
            retn = await s_genpkg.main(argv)
            self.eq(0, retn)

    def test_files(self):
        assets = s_files.getAssets()
        self.isin('test.dat', assets)

        s = s_files.getAssetStr('stormmod/common')
        self.isinstance(s, str)

        self.raises(ValueError, s_files.getAssetPath, 'newp.bin')
        self.raises(ValueError, s_files.getAssetPath,
                    '../../../../../../../../../etc/passwd')
