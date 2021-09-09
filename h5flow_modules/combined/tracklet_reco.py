import numpy as np
import numpy.ma as ma

import sklearn.cluster as cluster
import sklearn.decomposition as dcomp
from skimage.measure import LineModelND, ransac

from h5flow.core import H5FlowStage, resources


class TrackletReconstruction(H5FlowStage):
    '''
        Reconstructs "tracklets" or short, collinear track segments from hit
        data using DBSCAN and RANSAC. The track direction is estimated using
        a PCA fit.

        Parameters:
         - ``tracklet_dset_name``: ``str``, path to output dataset
         - ``t0_dset_name``: ``str``, path to input t0 dataset
         - ``hits_dset_name``: ``str``, path to input charge hits dataset
         - ``dbscan_eps``: ``float``, dbscan epsilon parameter
         - ``dbscan_min_samples``: ``int``, dbscan min neighbor points to consider as "core" point
         - ``ransac_min_samples``: ``int``, min points to run ransac algorithm
         - ``ransac_residual_threshold``: ``float``, max distance from trial axis
         - ``ransac_max_trials``: ``int``, number of ransac trials per cluster
         - ``max_iterations``: ``int``, max number of fitting iterations before giving up

        Both ``hits_dset_name`` and ``t0_dset_name`` are required in the cache.

        Requires Geometry, RunData, and Units resource in workflow.

        ``tracklets`` datatype::

            id          u4,     unique identifier
            theta       f8,     track inclination w.r.t anode
            phi         f8,     track orientation w.r.t anode
            xp          f8,     intersection of track with ``x=0,y=0`` plane [mm]
            yp          f8,     intersection of track with ``x=0,y=0`` plane [mm]
            nhit        i8,     number of hits in track
            q           f8,     charge sum [mV]
            ts_start    f8,     PPS timestamp of track start [crs ticks]
            ts_end      f8,     PPS timestamp of track end [crs ticks]
            residual    f8(3,)  average track fit error in (x,y,z) [mm]
            length      f8      track length [mm]
            start       f8(3,)  track start point (x,y,z) [mm]
            end         f8(3,)  track end point (x,y,z) [mm]

    '''
    class_version = '0.0.1'

    default_tracklet_dset_name = 'combined/tracklets'
    default_hits_dset_name = 'charge/hits'
    default_t0_dset_name = 'combined/t0'

    default_dbscan_eps = 25
    default_dbscan_min_samples = 5
    default_ransac_min_samples = 2
    default_ransac_residual_threshold = 8
    default_ransac_max_trials = 100
    default_max_iterations = 100

    tracklet_dtype = np.dtype([
        ('id', 'u4'),
        ('theta', 'f8'), ('phi', 'f8'),
        ('xp', 'f8'), ('yp', 'f8'),
        ('nhit', 'i8'), ('q', 'f8'),
        ('ts_start', 'f8'), ('ts_end', 'f8'),
        ('residual', 'f8', (3,)), ('length', 'f8'),
        ('start', 'f8', (3,)), ('end', 'f8', (3,))
    ])

    def __init__(self, **params):
        super(TrackletReconstruction, self).__init__(**params)

        self.tracklet_dset_name = params.get('tracklet_dset_name', self.default_tracklet_dset_name)
        self.hits_dset_name = params.get('hits_dset_name', self.default_hits_dset_name)
        self.t0_dset_name = params.get('t0_dset_name', self.default_t0_dset_name)

        self._dbscan_eps = params.get('dbscan_eps', self.default_dbscan_eps)
        self._dbscan_min_samples = params.get('dbscan_min_samples', self.default_dbscan_min_samples)
        self._ransac_min_samples = params.get('ransac_min_samples', self.default_ransac_min_samples)
        self._ransac_residual_threshold = params.get('ransac_residual_threshold', self.default_ransac_residual_threshold)
        self._ransac_max_trials = params.get('ransac_max_trials', self.default_ransac_max_trials)
        self.max_iterations = params.get('max_iterations', self.default_max_iterations)

        self.dbscan = cluster.DBSCAN(eps=self._dbscan_eps, min_samples=self._dbscan_min_samples)

    def init(self, source_name):
        self.data_manager.set_attrs(self.tracklet_dset_name,
                                    classname=self.classname,
                                    class_version=self.class_version,
                                    hits_dset=self.hits_dset_name,
                                    t0_dset=self.t0_dset_name,
                                    dbscan_eps=self._dbscan_eps,
                                    dbscan_min_samples=self._dbscan_min_samples,
                                    ransac_min_samples=self._ransac_min_samples,
                                    ransac_residual_threshold=self._ransac_residual_threshold,
                                    ransac_max_trials=self._ransac_max_trials,
                                    max_iterations=self.max_iterations
                                    )

        self.data_manager.create_dset(self.tracklet_dset_name, self.tracklet_dtype)
        self.data_manager.create_ref(self.tracklet_dset_name, self.hits_dset_name)
        self.data_manager.create_ref(source_name, self.tracklet_dset_name)

    def run(self, source_name, source_slice, cache):
        events = cache[source_name]                         # shape: (N,)
        t0 = cache[self.t0_dset_name]                       # shape: (N,1)
        hits = cache[self.hits_dset_name]                   # shape: (N,M)

        track_ids = self.find_tracks(hits, t0)
        tracks = self.calc_tracks(hits, t0, track_ids)
        n_tracks = np.count_nonzero(~tracks['id'].mask)
        tracks_mask = ~tracks['id'].mask

        tracks_slice = self.data_manager.reserve_data(self.tracklet_dset_name, n_tracks)
        np.place(tracks['id'], tracks_mask, np.r_[tracks_slice].astype('u4'))
        self.data_manager.write_data(self.tracklet_dset_name, tracks_slice, tracks[tracks_mask])

        # track -> hit ref
        track_ref_id = np.take_along_axis(tracks['id'], track_ids, axis=-1)
        mask = (~track_ref_id.mask) & (track_ids != -1) & (~hits['id'].mask)
        ref = np.c_[track_ref_id[mask], hits['id'][mask]]
        self.data_manager.write_ref(self.tracklet_dset_name, self.hits_dset_name, ref)

        # event -> track ref
        ev_id = np.broadcast_to(np.expand_dims(np.r_[source_slice], axis=-1), tracks.shape)
        ref = np.c_[ev_id[tracks_mask], tracks['id'][tracks_mask]]
        self.data_manager.write_ref(source_name, self.tracklet_dset_name, ref)

    @staticmethod
    def hit_xyz(hits, t0):
        drift_t = hits['ts'] - t0['ts']
        drift_d = drift_t * (resources['LArData'].v_drift * resources['RunData'].crs_ticks)

        z = resources['Geometry'].get_z_coordinate(hits['iogroup'], hits['iochannel'], drift_d)

        xyz = np.concatenate((
            np.expand_dims(hits['px'], axis=-1),
            np.expand_dims(hits['py'], axis=-1),
            np.expand_dims(z, axis=-1),
        ), axis=-1)
        return xyz

    def find_tracks(self, hits, t0):
        '''
            Extract tracks from a given hits array

            :param hits: masked array ``shape: (N, n)``

            :param t0: masked array ``shape: (N, 1)``

            :returns: mask array ``shape: (N, n)`` of track ids for each hit, a value of -1 means no track is associated with the hit
        '''
        xyz = self.hit_xyz(hits, t0)

        iter_mask = np.ones(hits.shape, dtype=bool)
        iter_mask = iter_mask & (~hits['id'].mask)
        track_id = np.full(hits.shape, -1, dtype='i8')
        for i in range(hits.shape[0]):

            if not np.any(iter_mask[i]):
                continue

            current_track_id = -1

            for _ in range(self.max_iterations):
                # dbscan to find clusters
                track_ids = self._do_dbscan(xyz[i], iter_mask[i])

                for id_ in np.unique(track_ids):
                    if id_ == -1:
                        continue
                    mask = track_ids == id_
                    if np.sum(mask) <= self._ransac_min_samples:
                        continue

                    # ransac for collinear hits
                    inliers = self._do_ransac(xyz[i], mask)
                    mask[mask] = inliers

                    if np.sum(mask) < 1:
                        continue

                    # and a final dbscan for re-clustering
                    final_track_ids = self._do_dbscan(xyz[i], mask)

                    for id_ in np.unique(final_track_ids):
                        if id_ == -1:
                            continue
                        mask = final_track_ids == id_

                        current_track_id += 1
                        track_id[i, mask] = current_track_id
                        iter_mask[i, mask] = False

                if np.all(track_ids == -1) or not np.any(iter_mask[i]):
                    break

        return ma.array(track_id, mask=hits['id'].mask)

    @classmethod
    def calc_tracks(cls, hits, t0, track_ids):
        '''
            Calculate track parameters from hits and t0

            :param hits: masked array, ``shape: (N,M)``

            :param t0: masked array, ``shape: (N,1)``

            :param track_ids: masked array, ``shape: (N,M)``

            :returns: masked array, ``shape: (N,m)``
        '''
        xyz = cls.hit_xyz(hits, t0)

        n_tracks = np.clip(track_ids.max() + 1, 1, np.inf).astype(int) if np.count_nonzero(~track_ids.mask) \
            else 1
        tracks = np.empty((len(t0), n_tracks), dtype=cls.tracklet_dtype)
        tracks_mask = np.ones(tracks.shape, dtype=bool)
        for i in range(tracks.shape[0]):
            for j in range(tracks.shape[1]):
                mask = ((track_ids[i] == j) & (~track_ids.mask[i])
                        & (~hits['id'].mask[i]))
                if np.count_nonzero(mask) < 2:
                    continue

                # PCA on central hits
                centroid, axis = cls.do_pca(xyz[i], mask)
                r_min, r_max = cls.projected_limits(
                    centroid, axis, xyz[i][mask])
                residual = cls.track_residual(centroid, axis, xyz[i][mask])
                xyp = cls.xyp(axis, centroid)

                tracks[i, j]['theta'] = cls.theta(axis)
                tracks[i, j]['phi'] = cls.phi(axis)
                tracks[i, j]['xp'] = xyp[0]
                tracks[i, j]['yp'] = xyp[1]
                tracks[i, j]['nhit'] = np.count_nonzero(mask)
                tracks[i, j]['q'] = np.sum(hits[i][mask]['q'])
                tracks[i, j]['ts_start'] = np.min(hits[i][mask]['ts'])
                tracks[i, j]['ts_end'] = np.max(hits[i][mask]['ts'])
                tracks[i, j]['residual'] = residual
                tracks[i, j]['length'] = np.linalg.norm(r_max - r_min)
                tracks[i, j]['start'] = r_min
                tracks[i, j]['end'] = r_max

                tracks_mask[i, j] = False

        return ma.array(tracks, mask=tracks_mask)

    def _do_dbscan(self, xyz, mask):
        '''
            :param xyz: ``shape: (N,3)`` array of precomputed 3D distances

            :param mask: ``shape: (N,)`` boolean array of valid positions (``True == valid``)

            :returns: ``shape: (N,)`` array of grouped track ids
        '''
        clustering = self.dbscan.fit(xyz[mask])
        track_ids = np.full(len(mask), -1)
        track_ids[mask] = clustering.labels_
        return track_ids

    def _do_ransac(self, xyz, mask):
        '''
            :param xyz: ``shape: (N,3)`` array of 3D positions

            :param mask: ``shape: (N,)`` boolean array of valid positions (``True == valid``)

            :returns: ``shape: (N,)`` boolean array of colinear positions
        '''
        model_robust, inliers = ransac(xyz[mask], LineModelND,
                                       min_samples=self._ransac_min_samples,
                                       residual_threshold=self._ransac_residual_threshold,
                                       max_trials=self._ransac_max_trials)
        return inliers

    @staticmethod
    def do_pca(xyz, mask):
        '''
            :param xyz: ``shape: (N,3)`` array of 3D positions

            :param mask: ``shape: (N,)`` boolean array of valid positions (``True == valid``)

            :returns: ``tuple`` of ``shape: (3,)``, ``shape: (3,)`` of centroid and central axis
        '''
        centroid = np.mean(xyz[mask], axis=0)
        pca = dcomp.PCA(n_components=1).fit(xyz[mask] - centroid)
        axis = pca.components_[0] / np.linalg.norm(pca.components_[0])
        return centroid, axis

    @staticmethod
    def projected_limits(centroid, axis, xyz):
        s = np.dot((xyz - centroid), axis)
        xyz_min, xyz_max = np.amin(xyz, axis=0), np.amax(xyz, axis=0)
        r_max = np.clip(centroid + axis * np.max(s), xyz_min, xyz_max)
        r_min = np.clip(centroid + axis * np.min(s), xyz_min, xyz_max)
        return r_min, r_max

    @staticmethod
    def track_residual(centroid, axis, xyz):
        s = np.dot((xyz - centroid), axis)
        res = np.abs(xyz - (centroid + np.outer(s, axis)))
        return np.mean(res, axis=0)

    @staticmethod
    def theta(axis):
        '''
            :param axis: array, ``shape: (3,)``

            :returns: angle of axis w.r.t z-axis
        '''
        return np.arctan2(np.linalg.norm(axis[:2]), axis[-1])

    @staticmethod
    def phi(axis):
        '''
            :param axis: array, ``shape: (3,)``

            :returns: orientation of axis about z-axis
        '''
        return np.arctan2(axis[1], axis[0])

    @staticmethod
    def xyp(axis, centroid):
        '''
            :param axis: array, ``shape: (3,)``

            :param centroid: array, ``shape: (3,)``

            :returns: x,y coordinate where line intersects ``x=0,y=0`` plane
        '''
        if axis[-1] == 0:
            return centroid[:2]
        s = -centroid[-1] / axis[-1]
        return (centroid + axis * s)[:2]