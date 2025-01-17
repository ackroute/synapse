import asyncio
import hashlib
import logging
import tempfile
import contextlib

import aiohttp
import aiohttp_socks

import synapse.exc as s_exc
import synapse.common as s_common

import synapse.lib.cell as s_cell
import synapse.lib.base as s_base
import synapse.lib.const as s_const
import synapse.lib.share as s_share
import synapse.lib.config as s_config
import synapse.lib.hashset as s_hashset
import synapse.lib.httpapi as s_httpapi
import synapse.lib.msgpack as s_msgpack
import synapse.lib.lmdbslab as s_lmdbslab
import synapse.lib.slabseqn as s_slabseqn

logger = logging.getLogger(__name__)

CHUNK_SIZE = 16 * s_const.mebibyte
MAX_SPOOL_SIZE = CHUNK_SIZE * 32  # 512 mebibytes
MAX_HTTP_UPLOAD_SIZE = 4 * s_const.tebibyte

class AxonHttpUploadV1(s_httpapi.StreamHandler):

    async def prepare(self):
        self.upfd = None

        if not await self.allowed(('axon', 'upload')):
            await self.finish()

        # max_body_size defaults to 100MB and requires a value
        self.request.connection.set_max_body_size(MAX_HTTP_UPLOAD_SIZE)

        self.upfd = await self.cell.upload()
        self.hashset = s_hashset.HashSet()

    async def data_received(self, chunk):
        if chunk is not None:
            await self.upfd.write(chunk)
            self.hashset.update(chunk)
            await asyncio.sleep(0)

    def on_finish(self):
        if self.upfd is not None and not self.upfd.isfini:
            self.cell.schedCoroSafe(self.upfd.fini())

    def on_connection_close(self):
        self.on_finish()

    async def _save(self):
        size, sha256b = await self.upfd.save()

        fhashes = {htyp: hasher.hexdigest() for htyp, hasher in self.hashset.hashes}

        assert sha256b == s_common.uhex(fhashes.get('sha256'))
        assert size == self.hashset.size

        fhashes['size'] = size

        return self.sendRestRetn(fhashes)

    async def post(self):
        '''
        Called after all data has been read.
        '''
        await self._save()
        return

    async def put(self):
        await self._save()
        return

class AxonHttpHasV1(s_httpapi.Handler):

    async def get(self, sha256):
        if not await self.allowed(('axon', 'has')):
            return
        resp = await self.cell.has(s_common.uhex(sha256))
        return self.sendRestRetn(resp)

reqValidAxonDel = s_config.getJsValidator({
    'type': 'object',
    'properties': {
        'sha256s': {
            'type': 'array',
            'items': {'type': 'string', 'pattern': '(?i)^[0-9a-f]{64}$'}
        },
    },
    'additionalProperties': False,
    'required': ['sha256s'],
})

class AxonHttpDelV1(s_httpapi.Handler):

    async def post(self):

        if not await self.allowed(('axon', 'del')):
            return

        body = self.getJsonBody(validator=reqValidAxonDel)
        if body is None:
            return

        sha256s = body.get('sha256s')
        hashes = [s_common.uhex(s) for s in sha256s]
        resp = await self.cell.dels(hashes)
        return self.sendRestRetn(tuple(zip(sha256s, resp)))

class AxonHttpBySha256V1(s_httpapi.Handler):

    async def get(self, sha256):

        if not await self.allowed(('axon', 'get')):
            return

        sha256b = s_common.uhex(sha256)

        self.set_header('Content-Type', 'application/octet-stream')
        self.set_header('Content-Disposition', 'attachment')

        try:
            async for byts in self.cell.get(sha256b):
                self.write(byts)
                await self.flush()
                await asyncio.sleep(0)

        except s_exc.NoSuchFile as e:
            self.set_status(404)
            self.sendRestErr('NoSuchFile', e.get('mesg'))

        return

    async def delete(self, sha256):

        if not await self.allowed(('axon', 'del')):
            return

        sha256b = s_common.uhex(sha256)
        if not await self.cell.has(sha256b):
            self.set_status(404)
            self.sendRestErr('NoSuchFile', f'SHA-256 not found: {sha256}')
            return

        resp = await self.cell.del_(sha256b)
        return self.sendRestRetn(resp)

class UpLoad(s_base.Base):
    '''
    An object used to manage uploads to the Axon.
    '''
    async def __anit__(self, axon):  # type: ignore

        await s_base.Base.__anit__(self)

        self.axon = axon
        self.fd = tempfile.SpooledTemporaryFile(max_size=MAX_SPOOL_SIZE)
        self.size = 0
        self.sha256 = hashlib.sha256()
        self.onfini(self._uploadFini)

    def _uploadFini(self):
        self.fd.close()

    def _reset(self):
        if self.fd._rolled or self.fd.closed:
            self.fd.close()
            self.fd = tempfile.SpooledTemporaryFile(max_size=MAX_SPOOL_SIZE)
        else:
            # If we haven't rolled over, this skips allocating new objects
            self.fd.truncate(0)
            self.fd.seek(0)
        self.size = 0
        self.sha256 = hashlib.sha256()

    async def write(self, byts):
        '''
        Write bytes to the Upload object.

        Args:
            byts (bytes): Bytes to write to the current Upload object.

        Returns:
            (None): Returns None.
        '''
        self.size += len(byts)
        self.sha256.update(byts)
        self.fd.write(byts)

    async def save(self):
        '''
        Save the currently uploaded bytes to the Axon.

        Notes:
            This resets the Upload object, so it can be reused.

        Returns:
            tuple(int, bytes): A tuple of sizes in bytes and the sha256 hash of the saved files.
        '''

        sha256 = self.sha256.digest()
        rsize = self.size

        if await self.axon.has(sha256):
            self._reset()
            return rsize, sha256

        def genr():

            self.fd.seek(0)

            while True:

                if self.isfini:
                    raise s_exc.IsFini()

                byts = self.fd.read(CHUNK_SIZE)
                if not byts:
                    return

                yield byts

        await self.axon.save(sha256, genr())

        self._reset()
        return rsize, sha256

class UpLoadShare(UpLoad, s_share.Share):  # type: ignore
    typename = 'upload'

    async def __anit__(self, axon, link):
        await UpLoad.__anit__(self, axon)
        await s_share.Share.__anit__(self, link, None)

class AxonApi(s_cell.CellApi, s_share.Share):  # type: ignore

    async def __anit__(self, cell, link, user):
        await s_cell.CellApi.__anit__(self, cell, link, user)
        await s_share.Share.__anit__(self, link, None)

    async def get(self, sha256):
        '''
        Get bytes of a file.

        Args:
            sha256 (bytes): The sha256 hash of the file in bytes.

        Examples:

            Get the bytes from an Axon and process them::

                buf = b''
                async for bytz in axon.get(sha256):
                    buf =+ bytz

                await dostuff(buf)

        Yields:
            bytes: Chunks of the file bytes.

        Raises:
            synapse.exc.NoSuchFile: If the file does not exist.
        '''
        await self._reqUserAllowed(('axon', 'get'))
        async for byts in self.cell.get(sha256):
            yield byts

    async def has(self, sha256):
        '''
        Check if the Axon has a file.

        Args:
            sha256 (bytes): The sha256 hash of the file in bytes.

        Returns:
            boolean: True if the Axon has the file; false otherwise.
        '''
        await self._reqUserAllowed(('axon', 'has'))
        return await self.cell.has(sha256)

    async def size(self, sha256):
        '''
        Get the size of a file in the Axon.

        Args:
            sha256 (bytes): The sha256 hash of the file in bytes.

        Returns:
            int: The size of the file, in bytes. If not present, None is returned.
        '''
        await self._reqUserAllowed(('axon', 'has'))
        return await self.cell.size(sha256)

    async def hashes(self, offs):
        '''
        Yield hash rows for files that exist in the Axon in added order starting at an offset.

        Args:
            offs (int): The index offset.

        Yields:
            (int, (bytes, int)): An index offset and the file SHA-256 and size.
        '''
        await self._reqUserAllowed(('axon', 'has'))
        async for item in self.cell.hashes(offs):
            yield item

    async def history(self, tick, tock=None):
        '''
        Yield hash rows for files that existing in the Axon after a given point in time.

        Args:
            tick (int): The starting time (in epoch milliseconds).
            tock (int): The ending time to stop iterating at (in epoch milliseconds).

        Yields:
            (int, (bytes, int)): A tuple containing time of the hash was added and the file SHA-256 and size.
        '''
        await self._reqUserAllowed(('axon', 'has'))
        async for item in self.cell.history(tick, tock=tock):
            yield item

    async def wants(self, sha256s):
        '''
        Get a list of sha256 values the axon does not have from a input list.

        Args:
            sha256s (list): A list of sha256 values as bytes.

        Returns:
            list: A list of bytes containing the sha256 hashes the Axon does not have.
        '''
        await self._reqUserAllowed(('axon', 'has'))
        return await self.cell.wants(sha256s)

    async def put(self, byts):
        '''
        Store bytes in the Axon.

        Args:
            byts (bytes): The bytes to store in the Axon.

        Notes:
            This API should not be used for files greater than 128 MiB in size.

        Returns:
            tuple(int, bytes): A tuple with the file size and sha256 hash of the bytes.
        '''
        await self._reqUserAllowed(('axon', 'upload'))
        return await self.cell.put(byts)

    async def puts(self, files):
        '''
        Store a set of bytes in the Axon.

        Args:
            files (list): A list of bytes to store in the Axon.

        Notes:
            This API should not be used for storing more than 128 MiB of bytes at once.

        Returns:
            list(tuple(int, bytes)): A list containing tuples of file size and sha256 hash of the saved bytes.
        '''
        await self._reqUserAllowed(('axon', 'upload'))
        return await self.cell.puts(files)

    async def upload(self):
        '''
        Get an Upload object.

        Notes:
            The UpLoad object should be used to manage uploads greater than 128 MiB in size.

        Examples:
            Use an UpLoad object to upload a file to the Axon::

                async with axonProxy.upload() as upfd:
                    # Assumes bytesGenerator yields bytes
                    async for byts in bytsgenerator():
                        upfd.write(byts)
                    upfd.save()

            Use a single UpLoad object to save multiple files::

                async with axonProxy.upload() as upfd:
                    for fp in file_paths:
                        # Assumes bytesGenerator yields bytes
                        async for byts in bytsgenerator(fp):
                            upfd.write(byts)
                        upfd.save()

        Returns:
            UpLoadShare: An Upload manager object.
        '''
        await self._reqUserAllowed(('axon', 'upload'))
        return await UpLoadShare.anit(self.cell, self.link)

    async def del_(self, sha256):
        '''
        Remove the given bytes from the Axon by sha256.

        Args:
            sha256 (bytes): The sha256, in bytes, to remove from the Axon.

        Returns:
            boolean: True if the file is removed; false if the file is not present.
        '''
        await self._reqUserAllowed(('axon', 'del'))
        return await self.cell.del_(sha256)

    async def dels(self, sha256s):
        '''
        Given a list of sha256 hashes, delete the files from the Axon.

        Args:
            sha256s (list): A list of sha256 hashes in bytes form.

        Returns:
            list: A list of booleans, indicating if the file was deleted or not.
        '''
        await self._reqUserAllowed(('axon', 'del'))
        return await self.cell.dels(sha256s)

    async def wget(self, url, params=None, headers=None, json=None, body=None, method='GET', ssl=True, timeout=None):
        '''
        Stream a file download directly into the Axon.

        Args:
            url (str): The URL to retrieve.
            params (dict): Additional parameters to add to the URL.
            headers (dict): Additional HTTP headers to add in the request.
            json: A JSON body which is included with the request.
            body: The body to be included in the request.
            method (str): The HTTP method to use.
            ssl (bool): Perform SSL verification.
            timeout (int): The timeout of the request, in seconds.

        Notes:
            The response body will be stored, regardless of the response code. The ``ok`` value in the reponse does not
            reflect that a status code, such as a 404, was encountered when retrieving the URL.

            The dictionary returned by this may contain the following values::

                {
                    'ok': <boolean> - False if there were exceptions retrieving the URL.
                    'url': <str> - The URL retrieved (which could have been redirected)
                    'code': <int> - The response code.
                    'mesg': <str> - An error message if there was an exception when retrieving the URL.
                    'headers': <dict> - The response headers as a dictionary.
                    'size': <int> - The size in bytes of the response body.
                    'hashes': {
                        'md5': <str> - The MD5 hash of the response body.
                        'sha1': <str> - The SHA1 hash of the response body.
                        'sha256': <str> - The SHA256 hash of the response body.
                        'sha512': <str> - The SHA512 hash of the response body.
                    }
                }

        Returns:
            dict: A information dictionary containing the results of the request.
        '''
        await self._reqUserAllowed(('axon', 'wget'))
        return await self.cell.wget(url, params=params, headers=headers, json=json, body=body, method=method, ssl=ssl, timeout=timeout)

    async def metrics(self):
        '''
        Get the runtime metrics of the Axon.

        Returns:
            dict: A dictionary of runtime data about the Axon.
        '''
        await self._reqUserAllowed(('axon', 'has'))
        return await self.cell.metrics()

    async def iterMpkFile(self, sha256):
        '''
        Yield items from a MsgPack (.mpk) file in the Axon.

        Args:
            sha256 (bytes): The sha256 hash of the file in bytes.

        Yields:
            Unpacked items from the bytes.
        '''
        await self._reqUserAllowed(('axon', 'get'))
        async for item in self.cell.iterMpkFile(sha256):
            yield item

class Axon(s_cell.Cell):

    cellapi = AxonApi

    confdefs = {
        'max:bytes': {
            'description': 'The maximum number of bytes that can be stored in the Axon.',
            'type': 'integer',
            'minimum': 1,
            'hidecmdl': True,
        },
        'max:count': {
            'description': 'The maximum number of files that can be stored in the Axon.',
            'type': 'integer',
            'minimum': 1,
            'hidecmdl': True,
        },
        'http:proxy': {
            'description': 'An aiohttp-socks compatible proxy URL to use in the wget API.',
            'type': 'string',
        },
    }

    async def __anit__(self, dirn, conf=None):  # type: ignore

        await s_cell.Cell.__anit__(self, dirn, conf=conf)

        # share ourself via the cell dmon as "axon"
        # for potential default remote use
        self.dmon.share('axon', self)

        path = s_common.gendir(self.dirn, 'axon.lmdb')
        self.axonslab = await s_lmdbslab.Slab.anit(path)
        self.sizes = self.axonslab.initdb('sizes')
        self.onfini(self.axonslab.fini)

        self.hashlocks = {}

        self.axonhist = s_lmdbslab.Hist(self.axonslab, 'history')
        self.axonseqn = s_slabseqn.SlabSeqn(self.axonslab, 'axonseqn')

        node = await self.hive.open(('axon', 'metrics'))
        self.axonmetrics = await node.dict()
        self.axonmetrics.setdefault('size:bytes', 0)
        self.axonmetrics.setdefault('file:count', 0)

        self.maxbytes = self.conf.get('max:bytes')
        self.maxcount = self.conf.get('max:count')

        self.addHealthFunc(self._axonHealth)

        # modularize blob storage
        await self._initBlobStor()

        self._initAxonHttpApi()

    @contextlib.asynccontextmanager
    async def holdHashLock(self, hashbyts):
        '''
        A context manager that synchronizes edit access to a blob.

        Args:
            hashbyts (bytes): The blob to hold the lock for.
        '''

        item = self.hashlocks.get(hashbyts)
        if item is None:
            self.hashlocks[hashbyts] = item = [0, asyncio.Lock()]

        item[0] += 1
        async with item[1]:
            yield

        item[0] -= 1

        if item[0] == 0:
            self.hashlocks.pop(hashbyts, None)

    def _reqBelowLimit(self):

        if (self.maxbytes is not None and
            self.maxbytes <= self.axonmetrics.get('size:bytes')):
            mesg = f'Axon is at size:bytes limit: {self.maxbytes}'
            raise s_exc.HitLimit(mesg=mesg)

        if (self.maxcount is not None and
            self.maxcount <= self.axonmetrics.get('file:count')):
            mesg = f'Axon is at file:count limit: {self.maxcount}'
            raise s_exc.HitLimit(mesg=mesg)

    async def _axonHealth(self, health):
        health.update('axon', 'nominal', '', data=await self.metrics())

    async def _initBlobStor(self):
        path = s_common.gendir(self.dirn, 'blob.lmdb')
        self.blobslab = await s_lmdbslab.Slab.anit(path)
        self.blobs = self.blobslab.initdb('blobs')
        self.onfini(self.blobslab.fini)

    def _initAxonHttpApi(self):
        self.addHttpApi('/api/v1/axon/files/del', AxonHttpDelV1, {'cell': self})
        self.addHttpApi('/api/v1/axon/files/put', AxonHttpUploadV1, {'cell': self})
        self.addHttpApi('/api/v1/axon/files/has/sha256/([0-9a-fA-F]{64}$)', AxonHttpHasV1, {'cell': self})
        self.addHttpApi('/api/v1/axon/files/by/sha256/([0-9a-fA-F]{64}$)', AxonHttpBySha256V1, {'cell': self})

    def _addSyncItem(self, item):
        self.axonhist.add(item)
        self.axonseqn.add(item)

    async def history(self, tick, tock=None):
        '''
        Yield hash rows for files that existing in the Axon after a given point in time.

        Args:
            tick (int): The starting time (in epoch milliseconds).
            tock (int): The ending time to stop iterating at (in epoch milliseconds).

        Yields:
            (int, (bytes, int)): A tuple containing time of the hash was added and the file SHA-256 and size.
        '''
        for item in self.axonhist.carve(tick, tock=tock):
            yield item

    async def hashes(self, offs):
        '''
        Yield hash rows for files that exist in the Axon in added order starting at an offset.

        Args:
            offs (int): The index offset.

        Yields:
            (int, (bytes, int)): An index offset and the file SHA-256 and size.
        '''
        for item in self.axonseqn.iter(offs):
            if self.axonslab.has(item[1][0], db=self.sizes):
                yield item
            await asyncio.sleep(0)

    async def get(self, sha256):
        '''
        Get bytes of a file.

        Args:
            sha256 (bytes): The sha256 hash of the file in bytes.

        Examples:

            Get the bytes from an Axon and process them::

                buf = b''
                async for bytz in axon.get(sha256):
                    buf =+ bytz

                await dostuff(buf)

        Yields:
            bytes: Chunks of the file bytes.

        Raises:
            synapse.exc.NoSuchFile: If the file does not exist.
        '''
        if not await self.has(sha256):
            raise s_exc.NoSuchFile(mesg='Axon does not contain the requested file.', sha256=s_common.ehex(sha256))

        fhash = s_common.ehex(sha256)
        logger.debug(f'Getting blob [{fhash}].', extra=await self.getLogExtra(sha256=fhash))

        async for byts in self._get(sha256):
            yield byts

    async def _get(self, sha256):

        for _, byts in self.blobslab.scanByPref(sha256, db=self.blobs):
            yield byts

    async def put(self, byts):
        '''
        Store bytes in the Axon.

        Args:
            byts (bytes): The bytes to store in the Axon.

        Notes:
            This API should not be used for files greater than 128 MiB in size.

        Returns:
            tuple(int, bytes): A tuple with the file size and sha256 hash of the bytes.
        '''
        # Use a UpLoad context manager so that we can
        # ensure that a one-shot set of bytes is chunked
        # in a consistent fashion.
        async with await self.upload() as fd:
            await fd.write(byts)
            return await fd.save()

    async def puts(self, files):
        '''
        Store a set of bytes in the Axon.

        Args:
            files (list): A list of bytes to store in the Axon.

        Notes:
            This API should not be used for storing more than 128 MiB of bytes at once.

        Returns:
            list(tuple(int, bytes)): A list containing tuples of file size and sha256 hash of the saved bytes.
        '''
        return [await self.put(b) for b in files]

    async def upload(self):
        '''
        Get an Upload object.

        Notes:
            The UpLoad object should be used to manage uploads greater than 128 MiB in size.

        Examples:
            Use an UpLoad object to upload a file to the Axon::

                async with axon.upload() as upfd:
                    # Assumes bytesGenerator yields bytes
                    async for byts in bytsgenerator():
                        upfd.write(byts)
                    upfd.save()

            Use a single UpLoad object to save multiple files::

                async with axon.upload() as upfd:
                    for fp in file_paths:
                        # Assumes bytesGenerator yields bytes
                        async for byts in bytsgenerator(fp):
                            upfd.write(byts)
                        upfd.save()

        Returns:
            UpLoad: An Upload manager object.
        '''
        return await UpLoad.anit(self)

    async def has(self, sha256):
        '''
        Check if the Axon has a file.

        Args:
            sha256 (bytes): The sha256 hash of the file in bytes.

        Returns:
            boolean: True if the Axon has the file; false otherwise.
        '''
        return self.axonslab.get(sha256, db=self.sizes) is not None

    async def size(self, sha256):
        '''
        Get the size of a file in the Axon.

        Args:
            sha256 (bytes): The sha256 hash of the file in bytes.

        Returns:
            int: The size of the file, in bytes. If not present, None is returned.
        '''
        byts = self.axonslab.get(sha256, db=self.sizes)
        if byts is not None:
            return int.from_bytes(byts, 'big')

    async def metrics(self):
        '''
        Get the runtime metrics of the Axon.

        Returns:
            dict: A dictionary of runtime data about the Axon.
        '''
        return dict(self.axonmetrics.items())

    async def save(self, sha256, genr):
        '''
        Save a generator of bytes to the Axon.

        Args:
            sha256 (bytes): The sha256 hash of the file in bytes.
            genr: The bytes generator function.

        Returns:
            int: The size of the bytes saved.
        '''
        self._reqBelowLimit()

        async with self.holdHashLock(sha256):

            byts = self.axonslab.get(sha256, db=self.sizes)
            if byts is not None:
                return int.from_bytes(byts, 'big')

            fhash = s_common.ehex(sha256)
            logger.debug(f'Saving blob [{fhash}].', extra=await self.getLogExtra(sha256=fhash))

            size = await self._saveFileGenr(sha256, genr)

            self._addSyncItem((sha256, size))

            await self.axonmetrics.set('file:count', self.axonmetrics.get('file:count') + 1)
            await self.axonmetrics.set('size:bytes', self.axonmetrics.get('size:bytes') + size)

            self.axonslab.put(sha256, size.to_bytes(8, 'big'), db=self.sizes)

            return size

    async def _saveFileGenr(self, sha256, genr):
        size = 0
        for i, byts in enumerate(genr):
            size += len(byts)
            lkey = sha256 + i.to_bytes(8, 'big')
            self.blobslab.put(lkey, byts, db=self.blobs)
            await asyncio.sleep(0)
        return size

    async def dels(self, sha256s):
        '''
        Given a list of sha256 hashes, delete the files from the Axon.

        Args:
            sha256s (list): A list of sha256 hashes in bytes form.

        Returns:
            list: A list of booleans, indicating if the file was deleted or not.
        '''
        return [await self.del_(s) for s in sha256s]

    async def del_(self, sha256):
        '''
        Remove the given bytes from the Axon by sha256.

        Args:
            sha256 (bytes): The sha256, in bytes, to remove from the Axon.

        Returns:
            boolean: True if the file is removed; false if the file is not present.
        '''
        async with self.holdHashLock(sha256):

            byts = self.axonslab.pop(sha256, db=self.sizes)
            if not byts:
                return False

            fhash = s_common.ehex(sha256)
            logger.debug(f'Deleting blob [{fhash}].', extra=await self.getLogExtra(sha256=fhash))

            size = int.from_bytes(byts, 'big')
            await self.axonmetrics.set('file:count', self.axonmetrics.get('file:count') - 1)
            await self.axonmetrics.set('size:bytes', self.axonmetrics.get('size:bytes') - size)

            await self._delBlobByts(sha256)
            return True

    async def _delBlobByts(self, sha256):
        # remove the actual blobs...
        for lkey in self.blobslab.scanKeysByPref(sha256, db=self.blobs):
            self.blobslab.delete(lkey, db=self.blobs)
            await asyncio.sleep(0)

    async def wants(self, sha256s):
        '''
        Get a list of sha256 values the axon does not have from a input list.

        Args:
            sha256s (list): A list of sha256 values as bytes.

        Returns:
            list: A list of bytes containing the sha256 hashes the Axon does not have.
        '''
        return [s for s in sha256s if not await self.has(s)]

    async def iterMpkFile(self, sha256):
        '''
        Yield items from a MsgPack (.mpk) file in the Axon.

        Args:
            sha256 (str): The sha256 hash of the file as a string.

        Yields:
            Unpacked items from the bytes.
        '''
        unpk = s_msgpack.Unpk()
        async for byts in self.get(s_common.uhex(sha256)):
            for _, item in unpk.feed(byts):
                yield item

    async def wget(self, url, params=None, headers=None, json=None, body=None, method='GET', ssl=True, timeout=None):
        '''
        Stream a file download directly into the Axon.

        Args:
            url (str): The URL to retrieve.
            params (dict): Additional parameters to add to the URL.
            headers (dict): Additional HTTP headers to add in the request.
            json: A JSON body which is included with the request.
            body: The body to be included in the request.
            method (str): The HTTP method to use.
            ssl (bool): Perform SSL verification.
            timeout (int): The timeout of the request, in seconds.

        Notes:
            The response body will be stored, regardless of the response code. The ``ok`` value in the reponse does not
            reflect that a status code, such as a 404, was encountered when retrieving the URL.

            The dictionary returned by this may contain the following values::

                {
                    'ok': <boolean> - False if there were exceptions retrieving the URL.
                    'url': <str> - The URL retrieved (which could have been redirected)
                    'code': <int> - The response code.
                    'mesg': <str> - An error message if there was an exception when retrieving the URL.
                    'headers': <dict> - The response headers as a dictionary.
                    'size': <int> - The size in bytes of the response body.
                    'hashes': {
                        'md5': <str> - The MD5 hash of the response body.
                        'sha1': <str> - The SHA1 hash of the response body.
                        'sha256': <str> - The SHA256 hash of the response body.
                        'sha512': <str> - The SHA512 hash of the response body.
                    }
                }

        Returns:
            dict: A information dictionary containing the results of the request.
        '''
        logger.debug(f'Wget called for [{url}].', extra=await self.getLogExtra(url=url))

        connector = None
        proxyurl = self.conf.get('http:proxy')
        if proxyurl is not None:
            connector = aiohttp_socks.ProxyConnector.from_url(proxyurl)

        atimeout = aiohttp.ClientTimeout(total=timeout)

        async with aiohttp.ClientSession(connector=connector, timeout=atimeout) as sess:

            try:

                async with sess.request(method, url, headers=headers, params=params, json=json, data=body, ssl=ssl) as resp:

                    info = {
                        'ok': True,
                        'url': str(resp.url),
                        'code': resp.status,
                        'headers': dict(resp.headers),
                    }

                    hashset = s_hashset.HashSet()

                    async with await self.upload() as upload:

                        async for byts in resp.content.iter_chunked(CHUNK_SIZE):
                            await upload.write(byts)
                            hashset.update(byts)

                        size, _ = await upload.save()

                    info['size'] = size
                    info['hashes'] = dict([(n, s_common.ehex(h)) for (n, h) in hashset.digests()])

                    return info

            except asyncio.CancelledError:
                raise

            except Exception as e:
                exc = s_common.excinfo(e)
                mesg = exc.get('errmsg')
                if not mesg:
                    mesg = exc.get('err')

                return {
                    'ok': False,
                    'mesg': mesg,
                }
