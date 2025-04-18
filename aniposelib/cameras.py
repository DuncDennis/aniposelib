import cv2
import numpy as np
from copy import copy
from scipy.sparse import lil_matrix, dok_matrix
from scipy.linalg import inv
from scipy import optimize
from scipy import signal
from numba import jit
from collections import defaultdict, Counter
import toml
import itertools
from tqdm import trange
from pprint import pprint
import time

from .boards import merge_rows, extract_points, extract_rtvecs, get_video_params
from .utils import get_initial_extrinsics, make_M, get_rtvec, get_connections


@jit(nopython=True, parallel=True)
def triangulate_simple(points, camera_mats):
    num_cams = len(camera_mats)
    A = np.zeros((num_cams * 2, 4))
    for i in range(num_cams):
        x, y = points[i]
        mat = camera_mats[i]
        A[(i * 2) : (i * 2 + 1)] = x * mat[2] - mat[0]
        A[(i * 2 + 1) : (i * 2 + 2)] = y * mat[2] - mat[1]
    u, s, vh = np.linalg.svd(A, full_matrices=True)
    p3d = vh[-1]
    p3d = p3d[:3] / p3d[3]
    return p3d



@jit(nopython=True, parallel=True)
def triangulate_weighted(points, camera_mats, weights):
    """
    Reconstruct a 3D point from its weighted 2D projections using linear triangulation.

    For each camera view, the function constructs a pair of equations from the projection relation:
        x * P[2, :] - P[0, :] = 0
        y * P[2, :] - P[1, :] = 0
    These equations are scaled by a confidence weight (0 to 1) for each observation before stacking them
    into a matrix A. The 3D point in homogeneous coordinates is found via SVD, where the solution corresponds
    to the singular vector associated with the smallest singular value. Finally, the homogeneous coordinate is
    converted to a Euclidean point.

    Parameters
    ----------
    points : numpy.ndarray, shape (num_cams, 2)
        The 2D coordinates for each view.
    camera_mats : numpy.ndarray, shape (num_cams, 3, 4)
        The 3x4 camera projection matrices for each view.
    weights : numpy.ndarray, shape (num_cams,)
        Confidence weights for each 2D observation, with values between 0 and 1.

    Returns
    -------
    p3d : numpy.ndarray, shape (3,)
        The reconstructed 3D point in Euclidean coordinates.
    """
    num_cams = len(camera_mats)
    # Allocate matrix A with 2 equations per camera view.
    A = np.zeros((num_cams * 2, 4))
    for i in range(num_cams):
        # Extract the 2D point (x, y) for the current camera view.
        x, y = points[i]
        # Get the current camera's projection matrix (3x4).
        mat = camera_mats[i]
        # Get the confidence weight for this observation.
        w = weights[i]
        # For the i-th camera, construct two equations scaled by the weight.
        # Equation from the x-coordinate:
        A[i * 2, :] = w * (x * mat[2, :] - mat[0, :])
        # Equation from the y-coordinate:
        A[i * 2 + 1, :] = w * (y * mat[2, :] - mat[1, :])

    # Compute the SVD of the weighted matrix A.
    u, s, vh = np.linalg.svd(A, full_matrices=True)
    # The solution is the singular vector corresponding to the smallest singular value.
    p3d_homogeneous = vh[-1]
    # Convert the homogeneous 4-vector into a Euclidean 3D point.
    p3d = p3d_homogeneous[:3] / p3d_homogeneous[3]
    return p3d


def get_error_dict(errors_full, min_points=10):
    n_cams = errors_full.shape[0]
    errors_norm = np.linalg.norm(errors_full, axis=2)

    good = ~np.isnan(errors_full[:, :, 0])

    error_dict = dict()

    for i in range(n_cams):
        for j in range(i + 1, n_cams):
            subset = good[i] & good[j]
            err_subset = errors_norm[:, subset][[i, j]]
            err_subset_mean = np.mean(err_subset, axis=0)
            if np.sum(subset) > min_points:
                percents = np.percentile(err_subset_mean, [15, 75])
                # percents = np.percentile(err_subset, [25, 75])
                error_dict[(i, j)] = (err_subset.shape[1], percents)
    return error_dict


def check_errors(cgroup, imgp):
    p3ds = cgroup.triangulate(imgp)
    errors_full = cgroup.reprojection_error(p3ds, imgp, mean=False)
    return get_error_dict(errors_full)


def subset_extra(extra, ixs):
    if extra is None:
        return None

    new_extra = {
        "objp": extra["objp"][ixs],
        "ids": extra["ids"][ixs],
        "rvecs": extra["rvecs"][:, ixs],
        "tvecs": extra["tvecs"][:, ixs],
    }
    return new_extra


def resample_points_extra(imgp, extra, n_samp=25):
    n_cams, n_points, _ = imgp.shape
    ids = remap_ids(extra["ids"])
    n_ids = np.max(ids) + 1
    good = ~np.isnan(imgp[:, :, 0])
    ixs = np.arange(n_points)

    cam_counts = np.zeros((n_ids, n_cams), dtype="int64")
    for idnum in range(n_ids):
        cam_counts[idnum] = np.sum(good[:, ids == idnum], axis=1)
    cam_counts_random = cam_counts + np.random.random(size=cam_counts.shape)
    best_boards = np.argsort(-cam_counts_random, axis=0)

    cam_totals = np.zeros(n_cams, dtype="int64")

    include = set()
    for cam_num in range(n_cams):
        for board_id in best_boards[:, cam_num]:
            include.update(ixs[ids == board_id])
            cam_totals += cam_counts[board_id]
            if (
                cam_totals[cam_num] >= n_samp
                or cam_counts_random[board_id, cam_num] < 1
            ):
                break

    final_ixs = sorted(include)
    newp = imgp[:, final_ixs]
    extra = subset_extra(extra, final_ixs)
    return newp, extra


def resample_points(imgp, extra=None, n_samp=25):
    # if extra is not None:
    #     return resample_points_extra(imgp, extra, n_samp)

    n_cams = imgp.shape[0]
    good = ~np.isnan(imgp[:, :, 0])
    ixs = np.arange(imgp.shape[1])

    num_cams = np.sum(~np.isnan(imgp[:, :, 0]), axis=0)

    include = set()

    for i in range(n_cams):
        for j in range(i + 1, n_cams):
            subset = good[i] & good[j]
            n_good = np.sum(subset)
            if n_good > 0:
                ## pick points, prioritizing points seen by more cameras
                arr = np.copy(num_cams[subset]).astype("float64")
                arr += np.random.random(size=arr.shape)
                picked_ix = np.argsort(-arr)[:n_samp]
                picked = ixs[subset][picked_ix]
                include.update(picked)

    final_ixs = sorted(include)
    newp = imgp[:, final_ixs]
    extra = subset_extra(extra, final_ixs)
    return newp, extra


def medfilt_data(values, size=15):
    padsize = size + 5
    vpad = np.pad(values, (padsize, padsize), mode="reflect")
    vpadf = signal.medfilt(vpad, kernel_size=size)
    return vpadf[padsize:-padsize]


def nan_helper(y):
    return np.isnan(y), lambda z: z.nonzero()[0]


def interpolate_data(vals):
    nans, ix = nan_helper(vals)
    out = np.copy(vals)
    try:
        out[nans] = np.interp(ix(nans), ix(~nans), vals[~nans])
    except ValueError:
        out[:] = 0
    return out


def remap_ids(ids):
    unique_ids = np.unique(ids)
    ids_out = np.copy(ids)
    for i, num in enumerate(unique_ids):
        ids_out[ids == num] = i
    return ids_out


def transform_points(points, rvecs, tvecs):
    """Rotate points by given rotation vectors and translate.
    Rodrigues' rotation formula is used.
    """
    theta = np.linalg.norm(rvecs, axis=1)[:, np.newaxis]
    with np.errstate(invalid="ignore"):
        v = rvecs / theta
        v = np.nan_to_num(v)
    dot = np.sum(points * v, axis=1)[:, np.newaxis]
    cos_theta = np.cos(theta)
    sin_theta = np.sin(theta)

    rotated = (
        cos_theta * points + sin_theta * np.cross(v, points) + dot * (1 - cos_theta) * v
    )

    return rotated + tvecs


class Camera:
    def __init__(
        self,
        matrix=np.eye(3),
        dist=np.zeros(5),
        size=None,
        rvec=np.zeros(3),
        tvec=np.zeros(3),
        name=None,
        extra_dist=False,
    ):
        self.set_camera_matrix(matrix)
        self.set_distortions(dist)
        self.set_size(size)
        self.set_rotation(rvec)
        self.set_translation(tvec)
        self.set_name(name)
        self.extra_dist = extra_dist

    def get_dict(self):
        return {
            "name": self.get_name(),
            "size": list(self.get_size()),
            "matrix": self.get_camera_matrix().tolist(),
            "distortions": self.get_distortions().tolist(),
            "rotation": self.get_rotation().tolist(),
            "translation": self.get_translation().tolist(),
        }

    def load_dict(self, d):
        self.set_camera_matrix(d["matrix"])
        self.set_rotation(d["rotation"])
        self.set_translation(d["translation"])
        self.set_distortions(d["distortions"])
        self.set_name(d["name"])
        self.set_size(d["size"])

    def from_dict(d):
        cam = Camera()
        cam.load_dict(d)
        return cam

    def get_camera_matrix(self):
        return self.matrix

    def get_distortions(self):
        return self.dist

    def set_camera_matrix(self, matrix):
        self.matrix = np.array(matrix, dtype="float64")

    def set_focal_length(self, fx, fy=None):
        if fy is None:
            fy = fx
        self.matrix[0, 0] = fx
        self.matrix[1, 1] = fy

    def get_focal_length(self, both=False):
        fx = self.matrix[0, 0]
        fy = self.matrix[1, 1]
        if both:
            return (fx, fy)
        else:
            return (fx + fy) / 2.0

    def get_cx_cy(self):  # optical center
        return self.matrix[0, 2], self.matrix[1, 2]

    def get_s(self):  # skewness parameter
        return self.matrix[0, 1]

    def set_distortions(self, dist):
        self.dist = np.array(dist, dtype="float64").ravel()

    def zero_distortions(self):
        self.dist = self.dist * 0

    def set_rotation(self, rvec):
        self.rvec = np.array(rvec, dtype="float64").ravel()

    def get_rotation(self):
        return self.rvec

    def set_translation(self, tvec):
        self.tvec = np.array(tvec, dtype="float64").ravel()

    def get_translation(self):
        return self.tvec

    def get_extrinsics_mat(self):
        return make_M(self.rvec, self.tvec)

    def get_name(self):
        return self.name

    def set_name(self, name):
        self.name = str(name)

    def set_size(self, size):
        """set size as (width, height)"""
        self.size = size

    def get_size(self):
        """get size as (width, height)"""
        return self.size

    def resize_camera(self, scale):
        """resize the camera by scale factor, updating intrinsics to match"""
        size = self.get_size()
        new_size = size[0] * scale, size[1] * scale
        matrix = self.get_camera_matrix()
        new_matrix = matrix * scale
        new_matrix[2, 2] = 1
        self.set_size(new_size)
        self.set_camera_matrix(new_matrix)

    def get_params_old(self, only_extrinsics=False):
        if only_extrinsics:
            params = np.zeros(6, dtype="float64")
        else:
            params = np.zeros(8 + self.extra_dist, dtype="float64")
        params[0:3] = self.get_rotation()
        params[3:6] = self.get_translation()
        if only_extrinsics:
            return params
        params[6] = self.get_focal_length()
        dist = self.get_distortions()
        params[7] = dist[0]
        if self.extra_dist:
            params[8] = dist[1]
        return params
    
    def get_params(self, optimize_intrinsics=False):
        """
        Return this camera's parameters as a 1D array.
        If optimize_intrinsics=False, we only return 6 extrinsics.
        If True, we return 16: extrinsics (6) + intrinsics (5) + distortion (5).
        """
        # Extrinsics
        out = np.zeros(6, dtype=np.float64)
        out[:3] = self.rvec
        out[3:6] = self.tvec

        if not optimize_intrinsics:
            return out

        # Intrinsics: fx, fy, cx, cy, skew
        # Distortion: k1, k2, p1, p2, k3
        intr_dist = np.zeros(10, dtype=np.float64)
        intr_dist[0] = self.matrix[0, 0]  # fx
        intr_dist[1] = self.matrix[1, 1]  # fy
        intr_dist[2] = self.matrix[0, 2]  # cx
        intr_dist[3] = self.matrix[1, 2]  # cy
        intr_dist[4] = self.matrix[0, 1]  # skew
        intr_dist[5:10] = self.dist[:5]   # k1, k2, p1, p2, k3

        return np.concatenate([out, intr_dist])

    def set_params_old(self, params, only_extrinsics=False):
        self.set_rotation(params[0:3])
        self.set_translation(params[3:6])
        if only_extrinsics:
            return

        self.set_focal_length(params[6])

        dist = np.zeros(5, dtype="float64")
        dist[0] = params[7]
        if self.extra_dist:
            dist[1] = params[8]
        self.set_distortions(dist)
    
    def set_params(self, params, optimize_intrinsics=False):
        """
        Set camera parameters from a 1D array of floats.
        If optimize_intrinsics=False, parse the first 6 as extrinsics.
        If True, parse extrinsics(6) + intr+dist(10) = 16.
        """
        self.rvec = params[:3].copy()
        self.tvec = params[3:6].copy()

        if not optimize_intrinsics:
            return

        # intrinsics + distortion
        fx = params[6]
        fy = params[7]
        cx = params[8]
        cy = params[9]
        skew = params[10]
        k1, k2, p1, p2, k3 = params[11:16]

        # Rebuild self.matrix
        self.matrix = np.array([
            [fx,   skew, cx],
            [0.0,   fy,  cy],
            [0.0, 0.0,  1.0]
        ], dtype=np.float64)

        # Update distortion
        self.dist = np.array([k1, k2, p1, p2, k3], dtype=np.float64)

    def distort_points(self, points):
        shape = points.shape
        points = points.reshape(-1, 1, 2)
        new_points = np.dstack([points, np.ones((points.shape[0], 1, 1))])
        out, _ = cv2.projectPoints(
            new_points,
            np.zeros(3),
            np.zeros(3),
            self.matrix.astype("float64"),
            self.dist.astype("float64"),
        )
        return out.reshape(shape)

    def undistort_points(self, points):
        shape = points.shape
        points = points.reshape(-1, 1, 2)
        out = cv2.undistortPoints(
            points, self.matrix.astype("float64"), self.dist.astype("float64")
        )
        return out.reshape(shape)

    def project_old(self, points):
        points = points.reshape(-1, 1, 3)
        out, _ = cv2.projectPoints(
            points,
            self.rvec,
            self.tvec,
            self.matrix.astype("float64"),
            self.dist.astype("float64"),
        )
        return out
    
    def project(self, points_3d):
        """
        Projects Nx3 points using the camera parameters.
        points_3d shape: (N, 1, 3) or (N, 3)
        Returns shape (N, 2).
        """
        if len(points_3d.shape) == 2:
            points_3d = points_3d.reshape(-1, 1, 3)

        # Use OpenCV to project with current intrinsics/distortion.
        # Note: we must convert rvec/tvec to float64, same for self.matrix and self.dist.
        proj_2d, _ = cv2.projectPoints(
            points_3d,
            self.rvec.astype(np.float64),
            self.tvec.astype(np.float64),
            self.matrix.astype(np.float64),
            self.dist.astype(np.float64),
        )
        return proj_2d.reshape(-1, 2)

    def reprojection_error(self, p3d, p2d):
        proj = self.project(p3d).reshape(p2d.shape)
        return p2d - proj

    def copy(self):
        return Camera(
            matrix=self.get_camera_matrix().copy(),
            dist=self.get_distortions().copy(),
            size=self.get_size(),
            rvec=self.get_rotation().copy(),
            tvec=self.get_translation().copy(),
            name=self.get_name(),
            extra_dist=self.extra_dist,
        )


class FisheyeCamera(Camera):
    def __init__(
        self,
        matrix=np.eye(3),
        dist=np.zeros(4),
        size=None,
        rvec=np.zeros(3),
        tvec=np.zeros(3),
        name=None,
        extra_dist=False,
    ):
        self.set_camera_matrix(matrix)
        self.set_distortions(dist)
        self.set_size(size)
        self.set_rotation(rvec)
        self.set_translation(tvec)
        self.set_name(name)
        self.extra_dist = extra_dist

    def from_dict(d):
        cam = FisheyeCamera()
        cam.load_dict(d)
        return cam

    def get_dict(self):
        d = super().get_dict()
        d["fisheye"] = True
        return d

    def distort_points(self, points):
        shape = points.shape
        points = points.reshape(-1, 1, 2)
        new_points = np.dstack([points, np.ones((points.shape[0], 1, 1))])
        out, _ = cv2.fisheye.projectPoints(
            new_points,
            np.zeros(3),
            np.zeros(3),
            self.matrix.astype("float64"),
            self.dist.astype("float64"),
        )
        return out.reshape(shape)

    def undistort_points(self, points):
        shape = points.shape
        points = points.reshape(-1, 1, 2)
        out = cv2.fisheye.undistortPoints(
            points.astype("float64"),
            self.matrix.astype("float64"),
            self.dist.astype("float64"),
        )
        return out.reshape(shape)

    def project(self, points):
        points = points.reshape(-1, 1, 3)
        out, _ = cv2.fisheye.projectPoints(
            points,
            self.rvec,
            self.tvec,
            self.matrix.astype("float64"),
            self.dist.astype("float64"),
        )
        return out

    def set_params(self, params, only_extrinsics):
        self.set_rotation(params[0:3])
        self.set_translation(params[3:6])

        if only_extrinsics:
            return

        self.set_focal_length(params[6])

        dist = np.zeros(4, dtype="float64")
        dist[0] = params[7]
        if self.extra_dist:
            dist[1] = params[8]
        # dist[2] = params[9]
        # dist[3] = params[10]
        self.set_distortions(dist)

    def get_params(self, only_extrinsics=False):
        if only_extrinsics:
            params = np.zeros(6, dtype="float64")
        else:
            params = np.zeros(8 + self.extra_dist, dtype="float64")
        params[0:3] = self.get_rotation()
        params[3:6] = self.get_translation()
        if only_extrinsics:
            return params
        params[6] = self.get_focal_length()
        dist = self.get_distortions()
        params[7] = dist[0]
        if self.extra_dist:
            params[8] = dist[1]
        # params[9] = dist[2]
        # params[10] = dist[3]
        return params

    def copy(self):
        return FisheyeCamera(
            matrix=self.get_camera_matrix().copy(),
            dist=self.get_distortions().copy(),
            size=self.get_size(),
            rvec=self.get_rotation().copy(),
            tvec=self.get_translation().copy(),
            name=self.get_name(),
            extra_dist=self.extra_dist,
        )


class CameraGroup:
    def __init__(self, cameras, metadata={}):
        self.cameras = cameras
        self.metadata = metadata

    def subset_cameras(self, indices):
        cams = [self.cameras[ix].copy() for ix in indices]
        return CameraGroup(cams, self.metadata)

    def subset_cameras_names(self, names):
        cur_names = self.get_names()
        cur_names_dict = dict(zip(cur_names, range(len(cur_names))))
        indices = []
        for name in names:
            if name not in cur_names_dict:
                raise IndexError(
                    "name {} not part of camera names: {}".format(name, cur_names)
                )
            indices.append(cur_names_dict[name])
        return self.subset_cameras(indices)

    def project(self, points):
        """Given an Nx3 array of points, this returns an CxNx2 array of 2D points,
        where C is the number of cameras"""
        points = points.reshape(-1, 1, 3)
        n_points = points.shape[0]
        n_cams = len(self.cameras)

        out = np.empty((n_cams, n_points, 2), dtype="float64")
        for cnum, cam in enumerate(self.cameras):
            out[cnum] = cam.project(points).reshape(n_points, 2)

        return out

    def triangulate_weighted(
        self, points, undistort=True, progress=False, weights=None
    ):
        """
        Triangulate 3D points from multi-view 2D observations.

        Given an array of image points with shape (C, N, 2), where C is the number of cameras and N is the number
        of points, this method returns an array of 3D points with shape (N, 3). Each 3D point is computed by
        triangulating its 2D observations from the cameras using a weighted linear triangulation approach.

        Optional undistortion of points is performed using each camera's distortion model.
        Optionally, confidence weights for each observation can be provided:
        - If `weights` is None, all observations are assumed to be equally reliable.
        - If provided, weights can be given as a 1D array of length C (applied to all points) or as a 2D array of
            shape (C, N) with individual weights per observation.

        Parameters
        ----------
        points : numpy.ndarray
            An array of image points with shape (C, N, 2) where C is the number of cameras and N is the number
            of points, or shape (C, 2) for a single point.
        undistort : bool, optional
            If True, the function will undistort image points using the corresponding camera model (default: True).
        progress : bool, optional
            If True, a progress bar is displayed when processing multiple points (default: False).
        weights : numpy.ndarray or None, optional
            Confidence weights for the observations. If None, all observations are weighted by one.
            If provided, it can be a 1D array of length C or a 2D array of shape (C, N).

        Returns
        -------
        numpy.ndarray
            An array of triangulated 3D points with shape (N, 3) (or a single 3D point if input was a single point).
        """
        # Check that the number of cameras matches the first dimension of the points array.
        assert points.shape[0] == len(self.cameras), (
            "Invalid points shape, first dim should be equal to number of cameras "
            "({}), but shape is {}".format(len(self.cameras), points.shape)
        )

        one_point = False
        # If points are provided as a 2D array (C, 2) for a single point, reshape to (C, 1, 2).
        if len(points.shape) == 2:
            points = points.reshape(-1, 1, 2)
            one_point = True

        # Undistort points if required.
        if undistort:
            new_points = np.empty(points.shape)
            for cnum, cam in enumerate(self.cameras):
                # Copy points to satisfy underlying OpenCV functions.
                sub = np.copy(points[cnum])
                new_points[cnum] = cam.undistort_points(sub)
            points = new_points

        n_cams, n_points, _ = points.shape

        # Prepare output array: one 3D point per image point.
        out = np.empty((n_points, 3))
        out[:] = np.nan

        # Retrieve the camera projection (extrinsic) matrices for each camera.
        cam_mats = np.array([cam.get_extrinsics_mat() for cam in self.cameras])

        # Set up the iterator with an optional progress bar.
        iterator = trange(n_points, ncols=70) if progress else range(n_points)

        # Process each point.
        for ip in iterator:
            # Get the (x,y) observations for this point across all cameras.
            subp = points[:, ip, :]
            # Determine which observations are valid (non-NaN).
            good = ~np.isnan(subp[:, 0])
            if np.sum(good) >= 2:
                # Determine the weights for the valid observations.
                if weights is None:
                    # If no weights are provided, all observations have weight 1.
                    w = np.ones(np.sum(good))
                else:
                    if weights.ndim == 1:
                        # weights is a 1D array: same weights applied to all points.
                        w = weights[good]
                    elif weights.ndim == 2:
                        # weights is a 2D array with shape (n_cams, n_points).
                        w = weights[good, ip]
                    else:
                        raise ValueError("Weights array must be either 1D or 2D.")
                # Triangulate the point using the weighted triangulation function.
                out[ip] = triangulate_weighted(subp[good], cam_mats[good], w)

        # If only one point was provided, return a single 3D point instead of an array.
        if one_point:
            out = out[0]

        return out

    def triangulate(self, points, undistort=True, progress=False, fast=False):
        """Given an CxNx2 array, this returns an Nx3 array of points,
        where N is the number of points and C is the number of cameras"""

        assert points.shape[0] == len(self.cameras), (
            "Invalid points shape, first dim should be equal to"
            " number of cameras ({}), but shape is {}".format(
                len(self.cameras), points.shape
            )
        )

        one_point = False
        if len(points.shape) == 2:
            points = points.reshape(-1, 1, 2)
            one_point = True

        if undistort:
            new_points = np.empty(points.shape)
            for cnum, cam in enumerate(self.cameras):
                # must copy in order to satisfy opencv underneath
                sub = np.copy(points[cnum])
                new_points[cnum] = cam.undistort_points(sub)
            points = new_points

        n_cams, n_points, _ = points.shape

        if fast:
            cam_Rt_mats = np.array(
                [cam.get_extrinsics_mat()[:3] for cam in self.cameras]
            )

            p3d_allview_withnan = []
            for j1, j2 in itertools.combinations(range(n_cams), 2):
                pts1, pts2 = points[j1], points[j2]
                Rt1, Rt2 = cam_Rt_mats[j1], cam_Rt_mats[j2]
                tri = cv2.triangulatePoints(Rt1, Rt2, pts1.T, pts2.T)
                tri = tri[:3] / tri[3]
                p3d_allview_withnan.append(tri.T)
            p3d_allview_withnan = np.array(p3d_allview_withnan)
            out = np.nanmedian(p3d_allview_withnan, axis=0)

        else:
            out = np.empty((n_points, 3))
            out[:] = np.nan

            cam_mats = np.array([cam.get_extrinsics_mat() for cam in self.cameras])

            if progress:
                iterator = trange(n_points, ncols=70)
            else:
                iterator = range(n_points)

            for ip in iterator:
                subp = points[:, ip, :]
                good = ~np.isnan(subp[:, 0])
                if np.sum(good) >= 2:
                    out[ip] = triangulate_simple(subp[good], cam_mats[good])

        if one_point:
            out = out[0]

        return out

    def triangulate_possible(
        self, points, undistort=True, min_cams=2, progress=False, threshold=0.5
    ):
        """Given an CxNxPx2 array, this returns an Nx3 array of points
        by triangulating all possible points and picking the ones with
        best reprojection error
        where:
        C: number of cameras
        N: number of points
        P: number of possible options per point
        """

        assert points.shape[0] == len(self.cameras), (
            "Invalid points shape, first dim should be equal to"
            " number of cameras ({}), but shape is {}".format(
                len(self.cameras), points.shape
            )
        )

        n_cams, n_points, n_possible, _ = points.shape

        cam_nums, point_nums, possible_nums = np.where(~np.isnan(points[:, :, :, 0]))

        all_iters = defaultdict(dict)

        for cam_num, point_num, possible_num in zip(
            cam_nums, point_nums, possible_nums
        ):
            if cam_num not in all_iters[point_num]:
                all_iters[point_num][cam_num] = []
            all_iters[point_num][cam_num].append((cam_num, possible_num))

        for point_num in all_iters.keys():
            for cam_num in all_iters[point_num].keys():
                all_iters[point_num][cam_num].append(None)

        out = np.full((n_points, 3), np.nan, dtype="float64")
        picked_vals = np.zeros((n_cams, n_points, n_possible), dtype="bool")
        errors = np.zeros(n_points, dtype="float64")
        points_2d = np.full((n_cams, n_points, 2), np.nan, dtype="float64")

        if progress:
            iterator = trange(n_points, ncols=70)
        else:
            iterator = range(n_points)

        for point_ix in iterator:
            best_point = None
            best_error = 200

            n_cams_max = len(all_iters[point_ix])

            for picked in itertools.product(*all_iters[point_ix].values()):
                picked = [p for p in picked if p is not None]
                if len(picked) < min_cams and len(picked) != n_cams_max:
                    continue

                cnums = [p[0] for p in picked]
                xnums = [p[1] for p in picked]

                pts = points[cnums, point_ix, xnums]
                cc = self.subset_cameras(cnums)

                p3d = cc.triangulate(pts, undistort=undistort)
                err = cc.reprojection_error(p3d, pts, mean=True)

                if err < best_error:
                    best_point = {
                        "error": err,
                        "point": p3d[:3],
                        "points": pts,
                        "picked": picked,
                        "joint_ix": point_ix,
                    }
                    best_error = err
                    if best_error < threshold:
                        break

            if best_point is not None:
                out[point_ix] = best_point["point"]
                picked = best_point["picked"]
                cnums = [p[0] for p in picked]
                xnums = [p[1] for p in picked]
                picked_vals[cnums, point_ix, xnums] = True
                errors[point_ix] = best_point["error"]
                points_2d[cnums, point_ix] = best_point["points"]

        return out, picked_vals, points_2d, errors

    def triangulate_ransac(self, points, undistort=True, min_cams=2, progress=False):
        """Given an CxNx2 array, this returns an Nx3 array of points,
        where N is the number of points and C is the number of cameras"""

        assert points.shape[0] == len(self.cameras), (
            "Invalid points shape, first dim should be equal to"
            " number of cameras ({}), but shape is {}".format(
                len(self.cameras), points.shape
            )
        )

        n_cams, n_points, _ = points.shape

        points_ransac = points.reshape(n_cams, n_points, 1, 2)

        return self.triangulate_possible(
            points_ransac, undistort=undistort, min_cams=min_cams, progress=progress
        )

    @jit(parallel=True, forceobj=True)
    def reprojection_error(self, p3ds, p2ds, mean=False):
        """Given an Nx3 array of 3D points and an CxNx2 array of 2D points,
        where N is the number of points and C is the number of cameras,
        this returns an CxNx2 array of errors.
        Optionally mean=True, this averages the errors and returns array of length N of errors"""

        one_point = False
        if len(p3ds.shape) == 1 and len(p2ds.shape) == 2:
            p3ds = p3ds.reshape(1, 3)
            p2ds = p2ds.reshape(-1, 1, 2)
            one_point = True

        n_cams, n_points, _ = p2ds.shape
        assert p3ds.shape == (n_points, 3), (
            "shapes of 2D and 3D points are not consistent: 2D={}, 3D={}".format(
                p2ds.shape, p3ds.shape
            )
        )

        errors = np.empty((n_cams, n_points, 2))

        for cnum, cam in enumerate(self.cameras):
            errors[cnum] = cam.reprojection_error(p3ds, p2ds[cnum])

        if mean:
            errors_norm = np.linalg.norm(errors, axis=2)
            good = ~np.isnan(errors_norm)
            errors_norm[~good] = 0
            denom = np.sum(good, axis=0).astype("float64")
            denom[denom < 1.5] = np.nan
            errors = np.sum(errors_norm, axis=0) / denom

        if one_point:
            if mean:
                errors = float(errors[0])
            else:
                errors = errors.reshape(-1, 2)

        return errors

    def bundle_adjust_iter(
        self,
        p2ds,
        extra=None,
        n_iters=6,
        start_mu=15,
        end_mu=1,
        max_nfev=200,
        ftol=1e-4,
        n_samp_iter=200,
        n_samp_full=1000,
        error_threshold=0.3,
        only_extrinsics=False,
        verbose=False,
    ):
        """Given an CxNx2 array of 2D points,
        where N is the number of points and C is the number of cameras,
        this performs iterative bundle adjustsment to fine-tune the parameters of the cameras.
        That is, it performs bundle adjustment multiple times, adjusting the weights given to points
        to reduce the influence of outliers.
        This is inspired by the algorithm for Fast Global Registration by Zhou, Park, and Koltun
        """

        assert p2ds.shape[0] == len(self.cameras), (
            "Invalid points shape, first dim should be equal to"
            " number of cameras ({}), but shape is {}".format(
                len(self.cameras), p2ds.shape
            )
        )

        p2ds_full = p2ds
        extra_full = extra

        p2ds, extra = resample_points(p2ds_full, extra_full, n_samp=n_samp_full)
        error = self.average_error(p2ds, median=True)

        if verbose:
            print("error: ", error)

        mus = np.exp(np.linspace(np.log(start_mu), np.log(end_mu), num=n_iters))

        if verbose:
            print("n_samples: {}".format(n_samp_iter))

        for i in range(n_iters):
            p2ds, extra = resample_points(p2ds_full, extra_full, n_samp=n_samp_full)
            p3ds = self.triangulate(p2ds)
            errors_full = self.reprojection_error(p3ds, p2ds, mean=False)
            errors_norm = self.reprojection_error(p3ds, p2ds, mean=True)

            error_dict = get_error_dict(errors_full)
            max_error = 0
            min_error = 0
            for k, v in error_dict.items():
                num, percents = v
                max_error = max(percents[-1], max_error)
                min_error = max(percents[0], min_error)
            mu = max(min(max_error, mus[i]), min_error)

            good = errors_norm < mu
            extra_good = subset_extra(extra, good)
            p2ds_samp, extra_samp = resample_points(
                p2ds[:, good], extra_good, n_samp=n_samp_iter
            )

            error = np.median(errors_norm)

            if error < error_threshold:
                break

            if verbose:
                pprint(error_dict)
                print(
                    "error: {:.2f}, mu: {:.1f}, ratio: {:.3f}".format(
                        error, mu, np.mean(good)
                    )
                )

            self.bundle_adjust(
                p2ds_samp,
                extra_samp,
                loss="linear",
                ftol=ftol,
                max_nfev=max_nfev,
                only_extrinsics=only_extrinsics,
                verbose=verbose,
            )

        p2ds, extra = resample_points(p2ds_full, extra_full, n_samp=n_samp_full)
        p3ds = self.triangulate(p2ds)
        errors_full = self.reprojection_error(p3ds, p2ds, mean=False)
        errors_norm = self.reprojection_error(p3ds, p2ds, mean=True)
        error_dict = get_error_dict(errors_full)
        if verbose:
            pprint(error_dict)

        max_error = 0
        min_error = 0
        for k, v in error_dict.items():
            num, percents = v
            max_error = max(percents[-1], max_error)
            min_error = max(percents[0], min_error)
        mu = max(max(max_error, end_mu), min_error)

        good = errors_norm < mu
        extra_good = subset_extra(extra, good)
        self.bundle_adjust(
            p2ds[:, good],
            extra_good,
            loss="linear",
            ftol=ftol,
            max_nfev=max(200, max_nfev),
            only_extrinsics=only_extrinsics,
            verbose=verbose,
        )

        error = self.average_error(p2ds, median=True)

        p3ds = self.triangulate(p2ds)
        errors_full = self.reprojection_error(p3ds, p2ds, mean=False)
        error_dict = get_error_dict(errors_full)
        if verbose:
            pprint(error_dict)

        if verbose:
            print("error: ", error)

        return error

    def bundle_adjust(
        self,
        p2ds,
        extra=None,
        loss="linear",
        threshold=50,
        ftol=1e-4,
        max_nfev=1000,
        weights=None,
        start_params=None,
        only_extrinsics=False,
        verbose=True,
    ):
        """Given an CxNx2 array of 2D points,
        where N is the number of points and C is the number of cameras,
        this performs bundle adjustsment to fine-tune the parameters of the cameras"""

        assert p2ds.shape[0] == len(self.cameras), (
            "Invalid points shape, first dim should be equal to"
            " number of cameras ({}), but shape is {}".format(
                len(self.cameras), p2ds.shape
            )
        )

        if extra is not None:
            extra["ids_map"] = remap_ids(extra["ids"])

        x0, n_cam_params = self._initialize_params_bundle(p2ds, extra, only_extrinsics)

        if start_params is not None:
            x0 = start_params
            # n_cam_params = len(self.cameras[0].get_params(only_extrinsics))

        error_fun = self._error_fun_bundle

        jac_sparse = self._jac_sparsity_bundle(p2ds, n_cam_params, extra)

        f_scale = threshold
        opt = optimize.least_squares(
            error_fun,
            x0,
            jac_sparsity=jac_sparse,
            f_scale=f_scale,
            x_scale="jac",
            loss=loss,
            ftol=ftol,
            method="trf",
            tr_solver="lsmr",
            verbose=2 * verbose,
            max_nfev=max_nfev,
            args=(p2ds, n_cam_params, extra, only_extrinsics),
        )
        best_params = opt.x

        for i, cam in enumerate(self.cameras):
            a = i * n_cam_params
            b = (i + 1) * n_cam_params
            cam.set_params(best_params[a:b], only_extrinsics)

        error = self.average_error(p2ds)
        return error

    @jit(parallel=True, forceobj=True)
    def _error_fun_bundle(self, params, p2ds, n_cam_params, extra, only_extrinsics):
        """Error function for bundle adjustment"""
        good = ~np.isnan(p2ds)
        n_cams = len(self.cameras)

        for i in range(n_cams):
            cam = self.cameras[i]
            a = i * n_cam_params
            b = (i + 1) * n_cam_params
            cam.set_params(params[a:b], only_extrinsics)

        n_cams = len(self.cameras)
        sub = n_cam_params * n_cams
        n3d = p2ds.shape[1] * 3
        p3ds_test = params[sub : sub + n3d].reshape(-1, 3)
        errors = self.reprojection_error(p3ds_test, p2ds)
        errors_reproj = errors[good]

        if extra is not None:
            ids = extra["ids_map"]
            objp = extra["objp"]
            min_scale = np.min(objp[objp > 0])
            n_boards = int(np.max(ids)) + 1
            a = sub + n3d
            rvecs = params[a : a + n_boards * 3].reshape(-1, 3)
            tvecs = params[a + n_boards * 3 : a + n_boards * 6].reshape(-1, 3)
            expected = transform_points(objp, rvecs[ids], tvecs[ids])
            errors_obj = 2 * (p3ds_test - expected).ravel() / min_scale
        else:
            errors_obj = np.array([])

        return np.hstack([errors_reproj, errors_obj])

    def _jac_sparsity_bundle(self, p2ds, n_cam_params, extra):
        """Given an CxNx2 array of 2D points,
        where N is the number of points and C is the number of cameras,
        compute the sparsity structure of the jacobian for bundle adjustment"""

        point_indices = np.zeros(p2ds.shape, dtype="int32")
        cam_indices = np.zeros(p2ds.shape, dtype="int32")

        for i in range(p2ds.shape[1]):
            point_indices[:, i] = i

        for j in range(p2ds.shape[0]):
            cam_indices[j] = j

        good = ~np.isnan(p2ds)

        if extra is not None:
            ids = extra["ids_map"]
            n_boards = int(np.max(ids)) + 1
            total_board_params = n_boards * (3 + 3)  # rvecs + tvecs
        else:
            n_boards = 0
            total_board_params = 0

        n_cams = p2ds.shape[0]
        n_points = p2ds.shape[1]
        total_params_reproj = n_cams * n_cam_params + n_points * 3
        n_params = total_params_reproj + total_board_params

        n_good_values = np.sum(good)
        if extra is not None:
            n_errors = n_good_values + n_points * 3
        else:
            n_errors = n_good_values

        A_sparse = dok_matrix((n_errors, n_params), dtype="int16")

        cam_indices_good = cam_indices[good]
        point_indices_good = point_indices[good]

        # -- reprojection error --
        ix = np.arange(n_good_values)

        ## update camera params based on point error
        for i in range(n_cam_params):
            A_sparse[ix, cam_indices_good * n_cam_params + i] = 1

        ## update point position based on point error
        for i in range(3):
            A_sparse[ix, n_cams * n_cam_params + point_indices_good * 3 + i] = 1

        # -- match for the object points--
        if extra is not None:
            point_ix = np.arange(n_points)

            ## update all the camera parameters
            # A_sparse[n_good_values:n_good_values+n_points*3,
            #          0:n_cams*n_cam_params] = 1

            ## update board rotation and translation based on error from expected
            for i in range(3):
                for j in range(3):
                    A_sparse[
                        n_good_values + point_ix * 3 + i,
                        total_params_reproj + ids * 3 + j,
                    ] = 1
                    A_sparse[
                        n_good_values + point_ix * 3 + i,
                        total_params_reproj + n_boards * 3 + ids * 3 + j,
                    ] = 1

            ## update point position based on error from expected
            for i in range(3):
                A_sparse[
                    n_good_values + point_ix * 3 + i,
                    n_cams * n_cam_params + point_ix * 3 + i,
                ] = 1

        return A_sparse

    def _initialize_params_bundle(self, p2ds, extra, only_extrinsics):
        """Given an CxNx2 array of 2D points,
        where N is the number of points and C is the number of cameras,
        initializes the parameters for bundle adjustment"""

        cam_params = np.hstack(
            [cam.get_params(only_extrinsics) for cam in self.cameras]
        )
        n_cam_params = len(cam_params) // len(self.cameras)

        total_cam_params = len(cam_params)

        n_cams, n_points, _ = p2ds.shape
        assert n_cams == len(self.cameras), (
            "number of cameras in CameraGroup does not "
            "match number of cameras in 2D points given"
        )

        p3ds = self.triangulate(p2ds)

        if extra is not None:
            ids = extra["ids_map"]
            n_boards = int(np.max(ids[~np.isnan(ids)])) + 1
            total_board_params = n_boards * (3 + 3)  # rvecs + tvecs

            # initialize to 0
            rvecs = np.zeros((n_boards, 3), dtype="float64")
            tvecs = np.zeros((n_boards, 3), dtype="float64")

            if "rvecs" in extra and "tvecs" in extra:
                rvecs_all = extra["rvecs"]
                tvecs_all = extra["tvecs"]
                for board_num in range(n_boards):
                    point_id = np.where(ids == board_num)[0][0]
                    cam_ids_possible = np.where(~np.isnan(p2ds[:, point_id, 0]))[0]
                    cam_id = np.random.choice(cam_ids_possible)
                    M_cam = self.cameras[cam_id].get_extrinsics_mat()
                    M_board_cam = make_M(
                        rvecs_all[cam_id, point_id], tvecs_all[cam_id, point_id]
                    )
                    M_board = np.matmul(inv(M_cam), M_board_cam)
                    rvec, tvec = get_rtvec(M_board)
                    rvecs[board_num] = rvec
                    tvecs[board_num] = tvec

        else:
            total_board_params = 0

        x0 = np.zeros(total_cam_params + p3ds.size + total_board_params)
        x0[:total_cam_params] = cam_params
        x0[total_cam_params : total_cam_params + p3ds.size] = p3ds.ravel()

        if extra is not None:
            start_board = total_cam_params + p3ds.size
            x0[start_board : start_board + n_boards * 3] = rvecs.ravel()
            x0[start_board + n_boards * 3 : start_board + n_boards * 6] = tvecs.ravel()

        return x0, n_cam_params

    def optim_points(
        self,
        points,
        p3ds,
        constraints=[],
        constraints_weak=[],
        scale_smooth=4,
        scale_length=2,
        scale_length_weak=0.5,
        reproj_error_threshold=15,
        reproj_loss="soft_l1",
        n_deriv_smooth=1,
        scores=None,
        verbose=False,
        n_fixed=0,
    ):
        """
        Take in an array of 2D points of shape CxNxJx2,
        an array of 3D points of shape NxJx3,
        and an array of constraints of shape Kx2, where
        C: number of camera
        N: number of frames
        J: number of joints
        K: number of constraints

        This function creates an optimized array of 3D points of shape NxJx3.

        Example constraints:
        constraints = [[0, 1], [1, 2], [2, 3]]
        (meaning that lengths of segments 0->1, 1->2, 2->3 are all constant)

        """
        assert points.shape[0] == len(self.cameras), (
            "Invalid points shape, first dim should be equal to"
            " number of cameras ({}), but shape is {}".format(
                len(self.cameras), points.shape
            )
        )

        n_cams, n_frames, n_joints, _ = points.shape
        constraints = np.array(constraints)
        constraints_weak = np.array(constraints_weak)

        p3ds_intp = np.apply_along_axis(interpolate_data, 0, p3ds)

        p3ds_med = np.apply_along_axis(medfilt_data, 0, p3ds_intp, size=7)

        default_smooth = 1.0 / np.mean(np.abs(np.diff(p3ds_med, axis=0)))
        scale_smooth_full = scale_smooth * default_smooth

        t1 = time.time()

        x0 = self._initialize_params_triangulation(
            p3ds_intp, constraints, constraints_weak
        )

        x0[~np.isfinite(x0)] = 0

        if n_fixed > 0:
            p3ds_fixed = p3ds_intp[:n_fixed]
        else:
            p3ds_fixed = None

        jac = self._jac_sparsity_triangulation(
            points, constraints, constraints_weak, n_deriv_smooth
        )

        opt2 = optimize.least_squares(
            self._error_fun_triangulation,
            x0=x0,
            jac_sparsity=jac,
            loss="linear",
            ftol=1e-3,
            verbose=2 * verbose,
            args=(
                points,
                constraints,
                constraints_weak,
                scores,
                scale_smooth_full,
                scale_length,
                scale_length_weak,
                reproj_error_threshold,
                reproj_loss,
                n_deriv_smooth,
                p3ds_fixed,
            ),
        )

        p3ds_new2 = opt2.x[: p3ds.size].reshape(p3ds.shape)

        if n_fixed > 0:
            p3ds_new2 = np.vstack([p3ds_fixed, p3ds_new2[n_fixed:]])

        t2 = time.time()

        if verbose:
            print("optimization took {:.2f} seconds".format(t2 - t1))

        return p3ds_new2

    def optim_points_possible(
        self,
        points,
        p3ds,
        constraints=[],
        constraints_weak=[],
        scale_smooth=4,
        scale_length=2,
        scale_length_weak=0.5,
        reproj_error_threshold=15,
        reproj_loss="soft_l1",
        n_deriv_smooth=1,
        scores=None,
        verbose=False,
    ):
        """
        Take in an array of 2D points of shape CxNxJxPx2,
        an array of 3D points of shape NxJx3,
        and an array of constraints of shape Kx2, where
        C: number of camera
        N: number of frames
        J: number of joints
        P: number of possible options per point
        K: number of constraints

        This function creates an optimized array of 3D points of shape NxJx3.

        Example constraints:
        constraints = [[0, 1], [1, 2], [2, 3]]
        (meaning that lengths of segments 0->1, 1->2, 2->3 are all constant)

        """
        assert points.shape[0] == len(self.cameras), (
            "Invalid points shape, first dim should be equal to"
            " number of cameras ({}), but shape is {}".format(
                len(self.cameras), points.shape
            )
        )

        n_cams, n_frames, n_joints, n_possible, _ = points.shape
        constraints = np.array(constraints)
        constraints_weak = np.array(constraints_weak)

        p3ds_intp = np.apply_along_axis(interpolate_data, 0, p3ds)

        p3ds_med = np.apply_along_axis(medfilt_data, 0, p3ds_intp, size=7)

        default_smooth = 1.0 / np.mean(np.abs(np.diff(p3ds_med, axis=0)))
        scale_smooth_full = scale_smooth * default_smooth

        t1 = time.time()

        x0 = self._initialize_params_triangulation_possible(
            p3ds_intp,
            points,
            constraints=constraints,
            constraints_weak=constraints_weak,
        )

        print("getting jacobian...")
        jac = self._jac_sparsity_triangulation_possible(
            points,
            constraints=constraints,
            constraints_weak=constraints_weak,
            n_deriv_smooth=n_deriv_smooth,
        )

        beta = 5

        print("starting optimization...")
        opt2 = optimize.least_squares(
            self._error_fun_triangulation_possible,
            x0=x0,
            jac_sparsity=jac,
            loss="linear",
            ftol=1e-3,
            verbose=2 * verbose,
            args=(
                points,
                beta,
                constraints,
                constraints_weak,
                scores,
                scale_smooth_full,
                scale_length,
                scale_length_weak,
                reproj_error_threshold,
                reproj_loss,
                n_deriv_smooth,
            ),
        )
        params = opt2.x

        p3ds_new2 = params[: p3ds.size].reshape(p3ds.shape)

        bad = np.isnan(points[:, :, :, :, 0])
        all_bad = np.all(bad, axis=3)

        n_params_norm = p3ds.size + len(constraints) + len(constraints_weak)

        alphas = np.zeros((n_cams, n_frames, n_joints, n_possible), dtype="float64")
        alphas[~bad] = params[n_params_norm:]

        alphas_exp = np.exp(beta * alphas)
        alphas_exp[bad] = 0
        alphas_sum = np.sum(alphas_exp, axis=3)
        alphas_sum[all_bad] = 1
        alphas_norm = alphas_exp / alphas_sum[:, :, :, None]
        alphas_norm[bad] = np.nan

        t2 = time.time()

        if verbose:
            print("optimization took {:.2f} seconds".format(t2 - t1))

        return p3ds_new2, alphas_norm

    def triangulate_optim(
        self, points, init_ransac=False, init_progress=False, **kwargs
    ):
        """
        Take in an array of 2D points of shape CxNxJx2, and an array of constraints of shape Kx2, where
        C: number of camera
        N: number of frames
        J: number of joints
        K: number of constraints

        This function creates an optimized array of 3D points of shape NxJx3.

        Example constraints:
        constraints = [[0, 1], [1, 2], [2, 3]]
        (meaning that lengths of segments 0->1, 1->2, 2->3 are all constant)

        """

        assert points.shape[0] == len(self.cameras), (
            "Invalid points shape, first dim should be equal to"
            " number of cameras ({}), but shape is {}".format(
                len(self.cameras), points.shape
            )
        )

        n_cams, n_frames, n_joints, _ = points.shape
        # constraints = np.array(constraints)
        # constraints_weak = np.array(constraints_weak)

        points_shaped = points.reshape(n_cams, n_frames * n_joints, 2)
        if init_ransac:
            p3ds, picked, p2ds, errors = self.triangulate_ransac(
                points_shaped, progress=init_progress
            )
            points = p2ds.reshape(points.shape)
        else:
            p3ds = self.triangulate(points_shaped, progress=init_progress)
        p3ds = p3ds.reshape((n_frames, n_joints, 3))

        c = np.isfinite(p3ds[:, :, 0])
        if np.sum(c) < 20:
            print("warning: not enough 3D points to run optimization")
            return p3ds

        return self.optim_points(points, p3ds, **kwargs)

    @jit(forceobj=True, parallel=True)
    def _error_fun_triangulation(
        self,
        params,
        p2ds,
        constraints=[],
        constraints_weak=[],
        scores=None,
        scale_smooth=10000,
        scale_length=1,
        scale_length_weak=0.2,
        reproj_error_threshold=100,
        reproj_loss="soft_l1",
        n_deriv_smooth=1,
        p3ds_fixed=None,
    ):
        n_cams, n_frames, n_joints, _ = p2ds.shape

        n_3d = n_frames * n_joints * 3
        n_constraints = len(constraints)
        n_constraints_weak = len(constraints_weak)

        # load params
        p3ds = params[:n_3d].reshape((n_frames, n_joints, 3))
        joint_lengths = np.array(params[n_3d : n_3d + n_constraints])
        joint_lengths_weak = np.array(params[n_3d + n_constraints :])

        ## if fixed points, first n_fixed parameter points are ignored
        ## and replacement points are put in
        ## this way we can keep rest of code the same, especially _jac_sparsity_triangulation
        if p3ds_fixed is not None:
            n_fixed = p3ds_fixed.shape[0]
            p3ds = np.vstack([p3ds_fixed, p3ds[n_fixed:]])

        # reprojection errors
        p3ds_flat = p3ds.reshape(-1, 3)
        p2ds_flat = p2ds.reshape((n_cams, -1, 2))
        errors = self.reprojection_error(p3ds_flat, p2ds_flat)
        if scores is not None:
            scores_flat = scores.reshape((n_cams, -1))
            errors = errors * scores_flat[:, :, None]
        errors_reproj = errors[~np.isnan(p2ds_flat)]

        rp = reproj_error_threshold
        errors_reproj = np.abs(errors_reproj)
        if reproj_loss == "huber":
            bad = errors_reproj > rp
            errors_reproj[bad] = rp * (2 * np.sqrt(errors_reproj[bad] / rp) - 1)
        elif reproj_loss == "linear":
            pass
        elif reproj_loss == "soft_l1":
            errors_reproj = rp * 2 * (np.sqrt(1 + errors_reproj / rp) - 1)

        # temporal constraint
        errors_smooth = np.diff(p3ds, n=n_deriv_smooth, axis=0).ravel() * scale_smooth

        # joint length constraint
        errors_lengths = np.empty((n_constraints, n_frames), dtype="float64")
        for cix, (a, b) in enumerate(constraints):
            lengths = np.linalg.norm(p3ds[:, a] - p3ds[:, b], axis=1)
            expected = joint_lengths[cix]
            errors_lengths[cix] = 100 * (lengths - expected) / expected
        errors_lengths = errors_lengths.ravel() * scale_length

        errors_lengths_weak = np.empty((n_constraints_weak, n_frames), dtype="float64")
        for cix, (a, b) in enumerate(constraints_weak):
            lengths = np.linalg.norm(p3ds[:, a] - p3ds[:, b], axis=1)
            expected = joint_lengths_weak[cix]
            errors_lengths_weak[cix] = 100 * (lengths - expected) / expected
        errors_lengths_weak = errors_lengths_weak.ravel() * scale_length_weak

        return np.hstack(
            [errors_reproj, errors_smooth, errors_lengths, errors_lengths_weak]
        )

    def _error_fun_triangulation_possible(
        self, params, p2ds, beta=2, constraints=[], constraints_weak=[], *args
    ):
        # extract alphas from end of params
        # soft argmax for picking the appropriate points from p2ds
        # pass the points to error_fun_triangulate_possible for residuals
        # add errors to keep the alphas in check
        # return all the errors

        n_cams, n_frames, n_joints, n_possible, _ = p2ds.shape

        n_3d = n_frames * n_joints * 3
        n_constraints = len(constraints)
        n_constraints_weak = len(constraints_weak)
        n_params_norm = n_3d + n_constraints + n_constraints_weak

        # load params
        bad = np.isnan(p2ds[:, :, :, :, 0])
        all_bad = np.all(bad, axis=3)

        alphas = np.zeros((n_cams, n_frames, n_joints, n_possible), dtype="float64")
        alphas[~bad] = params[n_params_norm:]
        params_rest = np.array(params[:n_params_norm])

        # get normalized alphas
        alphas_exp = np.exp(beta * alphas)
        alphas_exp[bad] = 0
        alphas_sum = np.sum(alphas_exp, axis=3)
        alphas_sum[all_bad] = 1
        alphas_norm = alphas_exp / alphas_sum[:, :, :, None]

        # extract the 2D points using soft argmax
        p2ds_test = np.copy(p2ds)
        p2ds_test[bad] = 0
        p2ds_adj = np.sum(alphas_norm[:, :, :, :, None] * p2ds_test, axis=3)
        p2ds_adj[all_bad] = np.nan

        errors = self._error_fun_triangulation(
            params_rest, p2ds_adj, constraints, constraints_weak, *args
        )

        alphas_test = alphas_norm[~all_bad]
        errors_alphas = (1 - np.std(alphas_test, axis=1)) * 10

        return np.hstack([errors, errors_alphas])

    def _initialize_params_triangulation(
        self, p3ds, constraints=[], constraints_weak=[]
    ):
        joint_lengths = np.empty(len(constraints), dtype="float64")
        joint_lengths_weak = np.empty(len(constraints_weak), dtype="float64")

        for cix, (a, b) in enumerate(constraints):
            lengths = np.linalg.norm(p3ds[:, a] - p3ds[:, b], axis=1)
            joint_lengths[cix] = np.median(lengths)

        for cix, (a, b) in enumerate(constraints_weak):
            lengths = np.linalg.norm(p3ds[:, a] - p3ds[:, b], axis=1)
            joint_lengths_weak[cix] = np.median(lengths)

        all_lengths = np.hstack([joint_lengths, joint_lengths_weak])
        med = np.median(all_lengths)
        if med == 0:
            med = 1e-3

        mad = np.median(np.abs(all_lengths - med))

        joint_lengths[joint_lengths == 0] = med
        joint_lengths_weak[joint_lengths_weak == 0] = med
        joint_lengths[joint_lengths > med + mad * 5] = med
        joint_lengths_weak[joint_lengths_weak > med + mad * 5] = med

        return np.hstack([p3ds.ravel(), joint_lengths, joint_lengths_weak])

    def _initialize_params_triangulation_possible(self, p3ds, p2ds, **kwargs):
        # initialize params using above function
        # initialize alphas to 1 for first one and 0 for other possible

        n_cams, n_frames, n_joints, n_possible, _ = p2ds.shape
        good = ~np.isnan(p2ds[:, :, :, :, 0])

        alphas = np.zeros((n_cams, n_frames, n_joints, n_possible), dtype="float64")
        alphas[:, :, :, 0] = 0

        params = self._initialize_params_triangulation(p3ds, **kwargs)
        params_full = np.hstack([params, alphas[good]])

        return params_full

    def _jac_sparsity_triangulation(
        self, p2ds, constraints=[], constraints_weak=[], n_deriv_smooth=1
    ):
        n_cams, n_frames, n_joints, _ = p2ds.shape
        n_constraints = len(constraints)
        n_constraints_weak = len(constraints_weak)

        p2ds_flat = p2ds.reshape((n_cams, -1, 2))

        point_indices = np.zeros(p2ds_flat.shape, dtype="int32")
        for i in range(p2ds_flat.shape[1]):
            point_indices[:, i] = i

        point_indices_3d = np.arange(n_frames * n_joints).reshape((n_frames, n_joints))

        good = ~np.isnan(p2ds_flat)
        n_errors_reproj = np.sum(good)
        n_errors_smooth = (n_frames - n_deriv_smooth) * n_joints * 3
        n_errors_lengths = n_constraints * n_frames
        n_errors_lengths_weak = n_constraints_weak * n_frames

        n_errors = (
            n_errors_reproj + n_errors_smooth + n_errors_lengths + n_errors_lengths_weak
        )

        n_3d = n_frames * n_joints * 3
        n_params = n_3d + n_constraints + n_constraints_weak

        point_indices_good = point_indices[good]

        A_sparse = dok_matrix((n_errors, n_params), dtype="int16")

        # constraints for reprojection errors
        ix_reproj = np.arange(n_errors_reproj)
        for k in range(3):
            A_sparse[ix_reproj, point_indices_good * 3 + k] = 1

        # sparse constraints for smoothness in time
        frames = np.arange(n_frames - n_deriv_smooth)
        for j in range(n_joints):
            for n in range(n_deriv_smooth + 1):
                pa = point_indices_3d[frames, j]
                pb = point_indices_3d[frames + n, j]
                for k in range(3):
                    A_sparse[n_errors_reproj + pa * 3 + k, pb * 3 + k] = 1

        ## -- strong constraints --
        # joint lengths should change with joint lengths errors
        start = n_errors_reproj + n_errors_smooth
        frames = np.arange(n_frames)
        for cix, (a, b) in enumerate(constraints):
            A_sparse[start + cix * n_frames + frames, n_3d + cix] = 1

        # points should change accordingly to match joint lengths too
        frames = np.arange(n_frames)
        for cix, (a, b) in enumerate(constraints):
            pa = point_indices_3d[frames, a]
            pb = point_indices_3d[frames, b]
            for k in range(3):
                A_sparse[start + cix * n_frames + frames, pa * 3 + k] = 1
                A_sparse[start + cix * n_frames + frames, pb * 3 + k] = 1

        ## -- weak constraints --
        # joint lengths should change with joint lengths errors
        start = n_errors_reproj + n_errors_smooth + n_errors_lengths
        frames = np.arange(n_frames)
        for cix, (a, b) in enumerate(constraints_weak):
            A_sparse[start + cix * n_frames + frames, n_3d + n_constraints + cix] = 1

        # points should change accordingly to match joint lengths too
        frames = np.arange(n_frames)
        for cix, (a, b) in enumerate(constraints_weak):
            pa = point_indices_3d[frames, a]
            pb = point_indices_3d[frames, b]
            for k in range(3):
                A_sparse[start + cix * n_frames + frames, pa * 3 + k] = 1
                A_sparse[start + cix * n_frames + frames, pb * 3 + k] = 1

        return A_sparse

    def _jac_sparsity_triangulation_possible(self, p2ds_full, **kwargs):
        # initialize sparse jacobian using above function
        # extend to include alphas from parameters
        ## TODO: this initialization is really slow for some reason

        n_cams, n_frames, n_joints, n_possible, _ = p2ds_full.shape
        good_full = ~np.isnan(p2ds_full[:, :, :, :, 0])
        any_good = np.any(good_full, axis=3)

        n_alphas = np.sum(good_full)
        n_errors_alphas = np.sum(any_good)

        p2ds = p2ds_full[:, :, :, 0]
        A_sparse = self._jac_sparsity_triangulation(p2ds, **kwargs)

        n_errors, n_params = A_sparse.shape

        B_sparse = dok_matrix(
            (n_errors + n_errors_alphas, n_params + n_alphas), dtype="int16"
        )
        for r, c in zip(*A_sparse.nonzero()):
            B_sparse[r, c] = A_sparse[r, c]

        point_indices_2d = np.arange(n_cams * n_frames * n_joints).reshape(
            n_cams, n_frames, n_joints
        )
        point_indices_2d_rep = np.repeat(point_indices_2d[:, :, :, None], 2, axis=3)
        point_indices_2d_good = point_indices_2d_rep[~np.isnan(p2ds)]
        point_indices_good = point_indices_2d[any_good]

        alpha_indices = np.zeros(
            (n_cams, n_frames, n_joints, n_possible), dtype="int64"
        )
        for pnum in range(n_possible):
            alpha_indices[:, :, :, pnum] = point_indices_2d

        alpha_indices_good = alpha_indices[good_full]

        # alphas should change according to the reprojection error for each corresponding point
        point_indices_2d_good_find = defaultdict(list)
        for ix, p in enumerate(point_indices_2d_good):
            point_indices_2d_good_find[p].append(ix)

        for ix, alpha_index in enumerate(alpha_indices_good):
            B_sparse[point_indices_2d_good_find[alpha_index], n_params + ix] = 1

        # alphas should change according to the alpha errors
        point_indices_good_find = dict()
        for ix, p in enumerate(point_indices_good):
            point_indices_good_find[p] = ix

        for ix, alpha_index in enumerate(alpha_indices_good):
            if alpha_index in point_indices_good_find:
                err_ix = n_errors + point_indices_good_find[alpha_index]
                B_sparse[err_ix, n_params + ix] = 1

        return B_sparse

    def copy(self):
        cameras = [cam.copy() for cam in self.cameras]
        metadata = copy(self.metadata)
        return CameraGroup(cameras, metadata)

    def set_rotations(self, rvecs):
        for cam, rvec in zip(self.cameras, rvecs):
            cam.set_rotation(rvec)

    def set_translations(self, tvecs):
        for cam, tvec in zip(self.cameras, tvecs):
            cam.set_translation(tvec)

    def get_rotations(self):
        rvecs = []
        for cam in self.cameras:
            rvec = cam.get_rotation()
            rvecs.append(rvec)
        return np.array(rvecs)

    def get_translations(self):
        tvecs = []
        for cam in self.cameras:
            tvec = cam.get_translation()
            tvecs.append(tvec)
        return np.array(tvecs)

    def get_names(self):
        return [cam.get_name() for cam in self.cameras]

    def set_names(self, names):
        for cam, name in zip(self.cameras, names):
            cam.set_name(name)

    def average_error(self, p2ds, median=False):
        p3ds = self.triangulate(p2ds)
        errors = self.reprojection_error(p3ds, p2ds, mean=True)
        if median:
            return np.median(errors)
        else:
            return np.mean(errors)

    def calibrate_rows(
        self,
        all_rows,
        board,
        init_intrinsics=True,
        init_extrinsics=True,
        verbose=True,
        **kwargs,
    ):
        assert len(all_rows) == len(self.cameras), (
            "Number of camera detections does not match number of cameras"
        )

        for rows, camera in zip(all_rows, self.cameras):
            size = camera.get_size()

            assert size is not None, (
                "Camera with name {} has no specified frame size".format(
                    camera.get_name()
                )
            )

            if init_intrinsics:
                objp, imgp = board.get_all_calibration_points(rows)
                mixed = [(o, i) for (o, i) in zip(objp, imgp) if len(o) >= 9]
                objp, imgp = zip(*mixed)
                matrix = cv2.initCameraMatrix2D(objp, imgp, tuple(size))
                camera.set_camera_matrix(matrix.copy())
                camera.zero_distortions()

        print(self.get_dicts())

        for i, (row, cam) in enumerate(zip(all_rows, self.cameras)):
            all_rows[i] = board.estimate_pose_rows(cam, row)

        new_rows = [[r for r in rows if r["ids"].size >= 8] for rows in all_rows]
        merged = merge_rows(new_rows)
        imgp, extra = extract_points(merged, board, min_cameras=2)

        if init_extrinsics:
            rtvecs = extract_rtvecs(merged)
            if verbose:
                pprint(get_connections(rtvecs, self.get_names()))
            rvecs, tvecs = get_initial_extrinsics(rtvecs, self.get_names())
            self.set_rotations(rvecs)
            self.set_translations(tvecs)

        error = self.bundle_adjust_iter(imgp, extra, verbose=verbose, **kwargs)

        return error

    def get_rows_videos(self, videos, board, verbose=True):
        all_rows = []

        for cix, (cam, cam_videos) in enumerate(zip(self.cameras, videos)):
            rows_cam = []
            for vnum, vidname in enumerate(cam_videos):
                if verbose:
                    print(vidname)
                rows = board.detect_video(vidname, prefix=vnum, progress=verbose)
                if verbose:
                    print("{} boards detected".format(len(rows)))
                rows_cam.extend(rows)
            all_rows.append(rows_cam)

        return all_rows

    def set_camera_sizes_videos(self, videos):
        for cix, (cam, cam_videos) in enumerate(zip(self.cameras, videos)):
            rows_cam = []
            for vnum, vidname in enumerate(cam_videos):
                params = get_video_params(vidname)
                size = (params["width"], params["height"])
                cam.set_size(size)

    def calibrate_videos(
        self,
        videos,
        board,
        init_intrinsics=True,
        init_extrinsics=True,
        verbose=True,
        **kwargs,
    ):
        """Takes as input a list of list of video filenames, one list of each camera.
        Also takes a board which specifies what should be detected in the videos"""

        all_rows = self.get_rows_videos(videos, board, verbose=verbose)
        if init_extrinsics:
            self.set_camera_sizes_videos(videos)

        error = self.calibrate_rows(
            all_rows,
            board,
            init_intrinsics=init_intrinsics,
            init_extrinsics=init_extrinsics,
            verbose=verbose,
            **kwargs,
        )
        return error, all_rows

    def get_dicts(self):
        out = []
        for cam in self.cameras:
            out.append(cam.get_dict())
        return out

    def from_dicts(arr):
        cameras = []
        for d in arr:
            if "fisheye" in d and d["fisheye"]:
                cam = FisheyeCamera.from_dict(d)
            else:
                cam = Camera.from_dict(d)
            cameras.append(cam)
        return CameraGroup(cameras)

    def from_names(names, fisheye=False):
        cameras = []
        for name in names:
            if fisheye:
                cam = FisheyeCamera(name=name)
            else:
                cam = Camera(name=name)
            cameras.append(cam)
        return CameraGroup(cameras)

    def load_dicts(self, arr):
        for cam, d in zip(self.cameras, arr):
            cam.load_dict(d)

    def dump(self, fname):
        dicts = self.get_dicts()
        names = ["cam_{}".format(i) for i in range(len(dicts))]
        master_dict = dict(zip(names, dicts))
        master_dict["metadata"] = self.metadata
        with open(fname, "w") as f:
            toml.dump(master_dict, f)

    def load(fname):
        master_dict = toml.load(fname)
        keys = sorted(master_dict.keys())
        items = [master_dict[k] for k in keys if k != "metadata"]
        cgroup = CameraGroup.from_dicts(items)
        if "metadata" in master_dict:
            cgroup.metadata = master_dict["metadata"]
        return cgroup

    def resize_cameras(self, scale):
        for cam in self.cameras:
            cam.resize_camera(scale)

def _pack_params(camera_network, points_3d, optimize_intrinsics):
    """
    Create the initial parameter vector from (cameras + 3D points).
    If optimize_intrinsics=True, each camera has 16 parameters.
    Otherwise, 6.
    """
    n_cams = len(camera_network.cameras)
    T, K, _ = points_3d.shape  # 3D shape: (Time, Keypoints, 3)

    all_cam_params = []
    for cam in camera_network.cameras:
        p = cam.get_params(optimize_intrinsics=optimize_intrinsics)
        all_cam_params.append(p)

    cam_params_concat = np.concatenate(all_cam_params)
    n_cam_params = len(all_cam_params[0])  # either 6 or 16, typically

    # Flatten the 3D points: shape = (T*K, 3)
    p3d_flat = points_3d.reshape(-1, 3)

    # Concatenate into x0
    x0 = np.concatenate([cam_params_concat, p3d_flat.ravel()])
    return x0, n_cam_params

def _unpack_params(x_opt, camera_network, n_cam_params, optimize_intrinsics):
    """
    Extract camera parameters + 3D points from x_opt.
    Update camera_network in-place with new camera parameters.
    Return the new 3D points as (T, K, 3).
    """
    n_cams = len(camera_network.cameras)

    # Update each camera
    for i, cam in enumerate(camera_network.cameras):
        start = i * n_cam_params
        end = (i+1) * n_cam_params
        cam.set_params(x_opt[start:end], optimize_intrinsics=optimize_intrinsics)

    # The remainder is the 3D points
    offset = n_cams * n_cam_params
    # figure out how many 3D points are in the parameter vector
    # you might store T, K somewhere or pass them in
    # For example, let's assume you store them in camera_network for simplicity
    T, K = camera_network._shape_3d  # or pass them in another way
    p3d_size = T * K * 3
    p3d_flat = x_opt[offset : offset + p3d_size]
    p3d_new = p3d_flat.reshape((T, K, 3))
    return p3d_new



def bundle_adjust_with_weighted(
    camera_network,
    points_2d,
    init_points_3d,
    weights=None,
    optimize_intrinsics=True,
    max_nfev=100,
    ftol=1e-4,
    verbose=True, 
    loss="linear" 
):
    """
    Extended bundle adjustment that can also optimize intrinsics + distortion if optimize_intrinsics=True.

    Parameters
    ----------
    camera_network : CameraGroup
        Has cameras, each must implement get_params(...) and set_params(...).
    points_2d : np.ndarray of shape (C, T, K, 2)
        The 2D observations for each camera (C cameras, T frames, K keypoints).
        Missing data can be NaN.
    init_points_3d : np.ndarray of shape (T, K, 3)
        Initial guess for the 3D points.
    weights : np.ndarray or None, shape (C, T, K)
        Weight for each measurement. If None, all are 1.0.
    optimize_intrinsics : bool
        If True, we use 16 parameters per camera (extrinsics + intrinsics + 5 distortion).
        If False, only 6 extrinsics per camera are optimized.
    max_nfev : int
        Maximum iterations for the solver.
    ftol : float
        Tolerance for cost change termination.
    verbose : bool
        Print solver output.

    Returns
    -------
    p3d_opt : np.ndarray, shape (T, K, 3)
        The refined 3D points.
    camera_network : CameraGroup
        Cameras updated in-place with refined extrinsics (and possibly intrinsics).
    """

    # shape checks
    C, T, K, _ = points_2d.shape
    assert init_points_3d.shape == (T, K, 3), "Mismatch in 3D shape."

    # If you want, undistort the points here or handle that separately.

    # Build a mask of valid points
    mask_valid = ~np.isnan(points_2d[..., 0])

    # Make points to zero which are nan, but make the corresponding weight to zero. 
    nan_mask_points_3d = np.isnan(init_points_3d)
    init_points_3d, weights = fix_init_points_and_weights(init_points_3d, weights)

    # Pack initial param vector
    x0, n_cam_params = _pack_params(camera_network, init_points_3d, optimize_intrinsics)

    # For convenience, store T,K in the camera_network so fun(...) can retrieve them
    camera_network._shape_3d = (T, K)

    # Build the Jacobian sparsity pattern
    jac_sparsity = make_jac_sparsity(points_2d, mask_valid, n_cam_params)

    # Solve
    res = optimize.least_squares(
        fun=fun, 
        x0=x0,
        jac_sparsity=jac_sparsity,
        method="trf",
        verbose=2 if verbose else 0,
        x_scale="jac",
        ftol=ftol,
        loss=loss,
        max_nfev=max_nfev,
        args=(camera_network, points_2d, weights, n_cam_params, optimize_intrinsics, mask_valid), 
        bounds=(-np.inf, np.inf),
    )

    # Unpack final parameters
    x_opt = res.x
    p3d_opt = _unpack_params(x_opt, camera_network, n_cam_params, optimize_intrinsics)
    p3d_opt[nan_mask_points_3d] = np.nan
    return p3d_opt, camera_network

# @jit(forceobj=True, parallel=True)
def fun(
    x,
    camera_network,
    points_2d,
    weights,
    n_cam_params,
    optimize_intrinsics,
    mask_valid
):
    """
    Weighted reprojection residuals. Each valid 2D measurement yields 2 residuals.
    """
    
    n_cams = len(camera_network.cameras)
    T, K = camera_network._shape_3d  # or pass them in explicitly

    # 1) Update the camera parameters from x
    for i, cam in enumerate(camera_network.cameras):
        start = i * n_cam_params
        end = (i+1) * n_cam_params
        cam.set_params(x[start:end], optimize_intrinsics=optimize_intrinsics)

    # 2) Extract 3D points
    offset = n_cams * n_cam_params
    p3d_size = T * K * 3
    p3d_flat = x[offset : offset + p3d_size].reshape((T*K, 3))

    # 3) Build residuals
    # shape of points_2d: (C, T, K, 2)
    residuals = []

    for c in range(n_cams):
        cam = camera_network.cameras[c]
        for t in range(T):
            for k in range(K):
                if not mask_valid[c, t, k]:
                    continue
                idx_3d = t*K + k
                observed_2d = points_2d[c, t, k]

                # Project
                # shape = (1, 3)
                X = p3d_flat[idx_3d].reshape(1, 3)
                proj_2d = cam.project(X).ravel()  # shape (2,)

                # Weight
                w = 1.0
                if weights is not None:
                    w = weights[c, t, k]
                    if np.isnan(w):
                        w = 0.0

                # Weighted residual
                rx = w * (observed_2d[0] - proj_2d[0])
                ry = w * (observed_2d[1] - proj_2d[1])
                residuals.append(rx)
                residuals.append(ry)

    return np.array(residuals, dtype=np.float64)

def make_jac_sparsity(points_2d, mask_valid, n_cam_params):
    """
    Construct a sparse Jacobian pattern.
    For each valid measurement:
      - 2 residual rows
      - depends on n_cam_params columns for that camera
      - depends on 3 columns for that point
    """
    C, T, K, _ = points_2d.shape
    valid_count = np.sum(mask_valid)
    # total rows = valid_count * 2
    n_rows = valid_count * 2

    # total cameras = C
    # each camera has n_cam_params
    # total 3D points = T*K
    # each point has 3 params => total 3D param = 3 * T*K
    n_cam_total = C * n_cam_params
    n_points_total = T*K*3
    n_params = n_cam_total + n_points_total

    A = dok_matrix((n_rows, n_params), dtype=np.uint8)

    row_ptr = 0

    for c in range(C):
        for t in range(T):
            for k in range(K):
                if not mask_valid[c, t, k]:
                    continue

                # 2 residual rows
                r1 = row_ptr
                r2 = row_ptr + 1
                row_ptr += 2

                # camera block
                cam_start = c*n_cam_params
                cam_end = cam_start + n_cam_params
                # mark columns for [cam_start : cam_end] as 1
                for col in range(cam_start, cam_end):
                    A[r1, col] = 1
                    A[r2, col] = 1

                # 3D block
                p_idx = t*K + k
                pt_start = n_cam_total + p_idx*3
                pt_end   = pt_start + 3
                for col in range(pt_start, pt_end):
                    A[r1, col] = 1
                    A[r2, col] = 1

    return A


def fix_init_points_and_weights(init_points_3d, weights=None):
    """
    Replace any NaN 3D coordinates with zeros and set corresponding weights to zero.
    
    Parameters
    ----------
    init_points_3d : ndarray, shape (T, K, 3)
        The initial guess for 3D points (time T, keypoints K).
        Some may be NaN.
    weights : ndarray or None, shape (C, T, K), optional
        The per-camera weights for each 3D point. 
        If provided, the weights for any NaN 3D point get set to 0 for all cameras.

    Returns
    -------
    init_points_3d_fixed : ndarray, shape (T, K, 3)
        A copy of init_points_3d with NaNs replaced by 0.0
    weights_fixed : ndarray or None, shape (C, T, K)
        A copy of weights with zeros set for points that were NaN in init_points_3d.
        If weights was None, returns None.
    """
    # Make copies so we don't modify the originals in-place
    init_points_3d_fixed = np.copy(init_points_3d)
    weights_fixed = None if weights is None else np.copy(weights)

    # Find which (T,K) have NaN in any coordinate
    # shape: (T, K)
    nan_mask = np.isnan(init_points_3d_fixed[..., 0]) | \
               np.isnan(init_points_3d_fixed[..., 1]) | \
               np.isnan(init_points_3d_fixed[..., 2])

    # Replace NaNs in 3D with zeros
    init_points_3d_fixed[np.isnan(init_points_3d_fixed)] = 0.0

    # If we have a weights array, set those entries to 0
    if weights_fixed is not None:
        # weights_fixed has shape (C, T, K)
        # We want to set weights_fixed[:, t, k] = 0 for each (t,k) that was NaN
        T, K, _ = init_points_3d.shape
        C = weights_fixed.shape[0]

        for t in range(T):
            for k in range(K):
                if nan_mask[t, k]:
                    weights_fixed[:, t, k] = 0.0

    return init_points_3d_fixed, weights_fixed


def bundle_adjust_with_smoothness(
    camera_network,
    points_2d,
    init_points_3d,
    weights=None,
    optimize_intrinsics=True,
    smoothness_weight=None,
    smoothness_derivative="first",
    max_nfev=100,
    ftol=1e-4,
    verbose=True,
    loss="linear",
):
    """
    Perform bundle adjustment with an optional temporal smoothness penalty on 3D points.
    
    Parameters
    ----------
    camera_network : CameraGroup
        Contains cameras, each with get_params and set_params.
    points_2d : np.ndarray of shape (C, T, K, 2)
        The 2D observations for each camera (C cameras, T frames, K keypoints).
        Possibly containing NaNs for missing data.
    init_points_3d : np.ndarray of shape (T, K, 3)
        Initial guess for the 3D points over time (T) and keypoints (K).
    weights : np.ndarray or None, shape (C, T, K), optional
        Per-measurement confidence weights. If None, all are 1.
    optimize_intrinsics : bool
        If True, each camera has 16 parameters: extrinsics(6) + intrinsics(5) + distortion(5).
        If False, each camera has 6 parameters (extrinsics only).
    smoothness_weight : float or None
        If None, no smoothness penalty. If a float, we penalize frame-to-frame differences 
        (1st derivative) or acceleration (2nd derivative) in 3D.
    smoothness_derivative : str
        Either "first" or "second". Controls whether we do velocity or acceleration smoothing.
    max_nfev : int
        Max solver iterations.
    ftol : float
        Tolerance for cost function termination.
    verbose : bool
        If True, prints solver progress.
    loss : str
        SciPy’s robust loss type ("linear", "soft_l1", "huber", etc.).

    Returns
    -------
    p3d_opt : np.ndarray, shape (T, K, 3)
        Refined 3D points after BA.
    camera_network : CameraGroup
        The same camera group with updated parameters.
    """
    C, T, K, _ = points_2d.shape
    assert init_points_3d.shape == (T, K, 3), "init_points_3d shape mismatch"

    # 1) Identify valid 2D
    mask_valid = ~np.isnan(points_2d[..., 0])

    # 2) Fix up any NaNs in 3D and weights
    init_points_3d_fixed, weights_fixed = fix_init_points_and_weights(init_points_3d, weights)

    # 3) Build x0 (camera + 3D)
    x0, n_cam_params = _pack_params(camera_network, init_points_3d_fixed, optimize_intrinsics)

    # 4) Store shape so the residual function can see T,K
    camera_network._shape_3d = (T, K)

    # 5) Build standard reprojection sparsity
    jac_sparsity = make_jac_sparsity_with_smoothness(
        points_2d,
        mask_valid,
        n_cam_params,
        smoothness_weight,
        smoothness_derivative,
        T,
        K
    )
    # For advanced usage, you could also expand jac_sparsity to reflect smoothness terms,
    # but for simplicity we'll skip that here.

    # 6) Solve
    res = optimize.least_squares(
        fun=fun_with_smoothness,
        x0=x0,
        jac_sparsity=jac_sparsity,
        method="trf",
        verbose=2 if verbose else 0,
        x_scale="jac",
        ftol=ftol,
        loss=loss,
        max_nfev=max_nfev,
        bounds=(-np.inf, np.inf),
        args=(
            camera_network,
            points_2d,
            weights_fixed,
            n_cam_params,
            optimize_intrinsics,
            mask_valid,
            smoothness_weight,
            smoothness_derivative,
        ),
    )

    # 7) Unpack final params
    x_opt = res.x
    p3d_opt = _unpack_params(x_opt, camera_network, n_cam_params, optimize_intrinsics)

    # Restore original NaNs in final 3D
    p3d_opt[np.isnan(init_points_3d)] = np.nan

    return p3d_opt, camera_network


def fun_with_smoothness(
    x,
    camera_network,
    points_2d,
    weights,
    n_cam_params,
    optimize_intrinsics,
    mask_valid,
    smoothness_weight,
    smoothness_derivative
):
    """
    1) Standard weighted reprojection residual.
    2) Optional temporal smoothness penalty for consecutive frames (1st or 2nd derivative).
    """
    n_cams = len(camera_network.cameras)
    T, K = camera_network._shape_3d

    # -- 1) Update cameras --
    for i, cam in enumerate(camera_network.cameras):
        start = i * n_cam_params
        end = (i+1) * n_cam_params
        cam.set_params(x[start:end], optimize_intrinsics=optimize_intrinsics)

    # -- 2) Extract 3D from x --
    offset = n_cams * n_cam_params
    p3d_size = T*K*3
    p3d_flat = x[offset : offset + p3d_size].reshape((T*K, 3))
    p3d_matrix = p3d_flat.reshape(T, K, 3)

    # -- 3) Weighted reprojection residual (as in 'fun') --
    residuals = []
    for c in range(n_cams):
        cam = camera_network.cameras[c]
        for t in range(T):
            for k in range(K):
                if not mask_valid[c, t, k]:
                    continue
                idx_3d = t*K + k
                obs_2d = points_2d[c, t, k]
                proj_2d = cam.project(p3d_flat[idx_3d].reshape(1, 3)).ravel()

                # Weighted
                w = 1.0
                if weights is not None:
                    w = weights[c, t, k]
                    if np.isnan(w):
                        w = 0.0

                rx = w * (obs_2d[0] - proj_2d[0])
                ry = w * (obs_2d[1] - proj_2d[1])
                residuals.append(rx)
                residuals.append(ry)

    # -- 4) Smoothness penalty if requested --
    # skip if smoothness_weight is None or ~> 0
    if (smoothness_weight is not None) and (smoothness_weight > 1e-12):
        sqrt_sw = np.sqrt(smoothness_weight)

        if smoothness_derivative.lower() == "first":
            # p3d_matrix[t+1,k] - p3d_matrix[t,k]
            for t in range(T-1):
                for k in range(K):
                    # skip if either frame is NaN in 3D
                    if np.any(np.isnan(p3d_matrix[t, k])) or np.any(np.isnan(p3d_matrix[t+1, k])):
                        continue
                    diff = p3d_matrix[t+1, k] - p3d_matrix[t, k]
                    # Weighted by sqrt_sw
                    residuals.append(sqrt_sw * diff[0])
                    residuals.append(sqrt_sw * diff[1])
                    residuals.append(sqrt_sw * diff[2])

        elif smoothness_derivative.lower() == "second":
            # p3d_matrix[t+2,k] - 2*p3d_matrix[t+1,k] + p3d_matrix[t,k]
            for t in range(T-2):
                for k in range(K):
                    if (np.any(np.isnan(p3d_matrix[t, k])) or 
                        np.any(np.isnan(p3d_matrix[t+1, k])) or
                        np.any(np.isnan(p3d_matrix[t+2, k]))):
                        continue
                    accel = p3d_matrix[t+2, k] - 2*p3d_matrix[t+1, k] + p3d_matrix[t, k]
                    residuals.append(sqrt_sw * accel[0])
                    residuals.append(sqrt_sw * accel[1])
                    residuals.append(sqrt_sw * accel[2])

        # else: if user gave something else, ignore or raise an error

    # Return the final array
    return np.array(residuals, dtype=np.float64)


def make_jac_sparsity_with_smoothness(points_2d, mask_valid, n_cam_params,
                                      smoothness_weight, smoothness_derivative,
                                      T, K):
    """
    Like make_jac_sparsity, but we add extra rows for each temporal smoothness term.

    We assume:
    - points_2d shape = (C, T, K, 2)
    - 'mask_valid' is shape = (C, T, K)
    - 'n_cam_params' is # of camera params (6 or 16)
    - 'smoothness_weight' might be None or a float
    - 'smoothness_derivative' in ['first','second']
    - T, K are the #frames, #keypoints

    Returns
    -------
    A dok_matrix that has (#reproj_rows + #smoothness_rows) x (#camera_params + #3D_params).
    """
    # 1) Build the standard reprojection pattern:
    A_reproj = make_jac_sparsity(points_2d, mask_valid, n_cam_params)
    n_reproj_rows, n_params = A_reproj.shape

    # 2) If no smoothness, just return the standard version
    if smoothness_weight is None or smoothness_weight <= 1e-12:
        return A_reproj

    # 3) Count how many new rows we need for smoothness
    # e.g. for 'first' derivative, we have (T-1)*K * 3 additional rows
    if smoothness_derivative.lower() == "first":
        n_smooth_rows = (T - 1) * K * 3
    elif smoothness_derivative.lower() == "second":
        n_smooth_rows = (T - 2) * K * 3
    else:
        # If user gave something else, skip
        n_smooth_rows = 0

    # 4) Create a bigger DOK matrix for total rows
    A = dok_matrix((n_reproj_rows + n_smooth_rows, n_params), dtype=np.uint8)

    # 5) Copy the reprojection pattern into the top
    for (r, c), val in A_reproj.items():
        A[r, c] = val

    # 6) Now fill in the smoothness rows
    row_ptr = n_reproj_rows  # start adding new rows after reprojection
    sqrt_sw = np.sqrt(smoothness_weight)  # you won't actually store this, but indexing

    # We'll only need to mark 3D dependencies. The camera parameters do NOT matter for smoothness 
    # because the derivative constraints only depend on the 3D point coordinates.
    # So we do not set anything for columns in [0 : n_cams * n_cam_params].
    # The 3D block starts at 'cam_offset = C * n_cam_params'.

    # If you keep T,K in the camera group, you might not need them as function args.
    # We'll just assume T,K are known here.
    # index for 3D block
    # The total # 3D points = T*K, each has 3 coords => T*K*3
    n_cams = points_2d.shape[0]
    cam_offset = n_cams * n_cam_params

    # We'll do "first" derivative as an example:
    if smoothness_derivative.lower() == "first":
        for t in range(T-1):
            for k in range(K):
                # The row depends on p3d_matrix[t+1, k] and p3d_matrix[t, k].
                # That's 2 sets of x,y,z => 6 parameter columns total.
                # Each difference is 3 residuals (x,y,z).
                # We add 3 new rows in the matrix for them.

                # row for x, y, z
                # row_x = row_ptr
                # row_y = row_ptr + 1
                # row_z = row_ptr + 2
                # but we only need to mark columns with 1, not the smoothness_weight

                # 3D index => p_idx = t*K + k
                p_idxA = t*K + k
                p_idxB = (t+1)*K + k

                # The columns for p_idxA: (cam_offset + p_idxA*3) to (cam_offset + p_idxA*3 + 2)
                colA = cam_offset + p_idxA*3
                colB = cam_offset + p_idxB*3

                # Now set A[row_x, colA + 0] = 1 => dx/dx
                # But the difference is: p[t+1] - p[t].
                # That means the partial derivative w.r.t. each coordinate is +1 for p[t+1], -1 for p[t].
                # So we might store a +1 and -1. But since this code uses a binary pattern (1 = potentially non-zero),
                # it might suffice to set them to 1 or 2. Because the solver only wants to know "non-zero or zero?" 
                # There's no way to store negative 1 in the DOK pattern for jac_sparsity.

                # We'll just store 1 for each coordinate that is relevant, because "non-zero" is all that matters.
                # row_x
                A[row_ptr, colA + 0] = 1   # depends on p[t, k].x
                A[row_ptr, colB + 0] = 1   # depends on p[t+1, k].x

                # row_y
                A[row_ptr + 1, colA + 1] = 1
                A[row_ptr + 1, colB + 1] = 1

                # row_z
                A[row_ptr + 2, colA + 2] = 1
                A[row_ptr + 2, colB + 2] = 1

                row_ptr += 3

    elif smoothness_derivative.lower() == "second":
        # p[t+2] - 2p[t+1] + p[t], similarly each difference => 3 rows
        for t in range(T-2):
            for k in range(K):
                p_idxA = t*K + k       # p[t]
                p_idxB = (t+1)*K + k   # p[t+1]
                p_idxC = (t+2)*K + k   # p[t+2]

                colA = cam_offset + p_idxA*3
                colB = cam_offset + p_idxB*3
                colC = cam_offset + p_idxC*3

                # row_x
                A[row_ptr,   colA + 0] = 1
                A[row_ptr,   colB + 0] = 1
                A[row_ptr,   colC + 0] = 1
                # row_y
                A[row_ptr+1, colA + 1] = 1
                A[row_ptr+1, colB + 1] = 1
                A[row_ptr+1, colC + 1] = 1
                # row_z
                A[row_ptr+2, colA + 2] = 1
                A[row_ptr+2, colB + 2] = 1
                A[row_ptr+2, colC + 2] = 1

                row_ptr += 3

    return A