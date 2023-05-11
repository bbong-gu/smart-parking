# vim: expandtab:ts=4:sw=4
from __future__ import absolute_import
import numpy as np
from . import kalman_filter
from . import linear_assignment
from . import iou_matching
from .track import Track

class Tracker:
    """
    This is the multi-target tracker.
    Parameters
    ----------
    metric : nn_matching.NearestNeighborDistanceMetric
        A distance metric for measurement-to-track association.
    max_age : int
        Maximum number of missed misses before a track is deleted.
    n_init : int
        Number of consecutive detections before the track is confirmed. The
        track state is set to `Deleted` if a miss occurs within the first
        `n_init` frames.
    Attributes
    ----------
    metric : nn_matching.NearestNeighborDistanceMetric
        The distance metric used for measurement to track association.
    max_age : int
        Maximum number of missed misses before a track is deleted.
    n_init : int
        Number of frames that a track remains in initialization phase.
    kf : kalman_filter.KalmanFilter
        A Kalman filter to filter target trajectories in image space.
    tracks : List[Track]
        The list of active tracks at the current time step.
    """
    GATING_THRESHOLD = np.sqrt(kalman_filter.chi2inv95[4])

    def __init__(self, metric, max_iou_distance=0.9, max_age=30, n_init=3, _lambda=0):
        self.metric = metric
        self.max_iou_distance = max_iou_distance
        self.max_age = max_age
        self.n_init = n_init
        self._lambda = _lambda

        self.kf = kalman_filter.KalmanFilter()
        self.tracks = []
        self._next_id = 1
        self.n_person=0

    def predict(self):
        """Propagate track state distributions one time step forward.

        This function should be called once every time step, before `update`.
        """
        for track in self.tracks:
            track.predict(self.kf)

    def increment_ages(self):
        for track in self.tracks:
            track.increment_age()
            track.mark_missed()

    def update(self, detections, classes, confidences, n_parked_cars, parking_list, client_socket):
        """Perform measurement update and track management.

        Parameters
        ----------
        detections : List[deep_sort.detection.Detection]
            A list of detections at the current time step.

        """
        # Run matching cascade.
        matches, unmatched_tracks, unmatched_detections = self._match(detections)

        # Update track set.
        for track_idx, detection_idx in matches:
            self.tracks[track_idx].update(
                self.kf, detections[detection_idx], classes[detection_idx], confidences[detection_idx])
        for track_idx in unmatched_tracks: # Tracks whose object have disappeared
            self.tracks[track_idx].mark_missed()
        for detection_idx in unmatched_detections: # Objects without a track
            self._initiate_track(detections[detection_idx], classes[detection_idx].item(), 
            confidences[detection_idx].item(), n_parked_cars)

        # Custom code
        len_list = len(parking_list)
        if len_list == 4:
            parking_spaces=[[148,154],[268,156],[377,149],[498,151]]
        else:
            parking_spaces=[[151, 144], [238, 149], [329, 144], [410, 124], [539, 144]]
        i=0
        while i<len(self.tracks): # Delete lost objects
            if self.tracks[i].is_deleted():
                if self.tracks[i].class_id==0 and self.tracks[i].time_since_update>5: # If a person has disappeared
                    # Variables to find the car nearest the disappeared person
                    person_box = self.tracks[i].to_tlwh()
                    center_of_person_box=[int((person_box[0]+person_box[2])/2), int((person_box[1]+person_box[3])/2)]

                    # Find the parking space nearest the disappeared person
                    parking_space_gonna_be_empty=0
                    min_dx=9999
                    for j in range(len_list):
                        dx=center_of_person_box[0]-parking_spaces[j][0]
                        #dy=abs(center_of_person_box[1]-parking_spaces[j][1])
                        #min_dstance=dx+dy
                        # If a driver got in a car (excluding passengers)
                        if 0 < dx < 50 and dx<min_dx and center_of_person_box[1] < 150:
                            # Update most likely space
                            min_dx=dx
                            parking_space_gonna_be_empty=j
                            parking_list[parking_space_gonna_be_empty] = -parking_list[parking_space_gonna_be_empty]
                            print(parking_list,"going out")
                            if client_socket:
                                client_socket.sendall(str(parking_list).encode())
                                client_socket.recv(99) # Receive echo

                    #if min_distance != 9999 and parking_list[parking_space_gonna_be_empty] > 0: # A parking space found
                        # Find the car nearest the disappeared person
                        '''center_of_car_box=[0,0]
                        min_distance=9999
                        for j in range(len(self.tracks)):
                            if self.tracks[j].class_id==0: # Skip person
                                continue
                            car_box=self.tracks[j].to_tlwh()
                            center_of_car_box=[int(car_box[0]+car_box[2])/2, int(car_box[1]+car_box[3])/2]
                            dx=abs(center_of_person_box[0]-center_of_car_box[0])
                            dy=abs(center_of_person_box[1]-center_of_car_box[1])
                            distance=dx+dy # Distance between a person and the a parking space
                            # If a driver got in a car (excluding passengers)
                            if distance<150 and distance<min_distance:
                                # Update most likely space
                                min_distance=distance'''

                # Delete this track
                self.tracks[i].timer_alarm.timer_ended=True
                del self.tracks[i]
                continue
            i+=1

        #self.tracks = [t for t in self.tracks if not t.is_deleted()]

        # Update distance metric.
        active_targets = [t.track_id for t in self.tracks if t.is_confirmed()]
        features, targets = [], []
        for track in self.tracks:
            if not track.is_confirmed():
                continue
            features += track.features
            targets += [track.track_id for _ in track.features]
            track.features = []
        self.metric.partial_fit(np.asarray(features), np.asarray(targets), active_targets)

    def _full_cost_metric(self, tracks, dets, track_indices, detection_indices):
        """
        This implements the full lambda-based cost-metric. However, in doing so, it disregards
        the possibility to gate the position only which is provided by
        linear_assignment.gate_cost_matrix(). Instead, I gate by everything.
        Note that the Mahalanobis distance is itself an unnormalised metric. Given the cosine
        distance being normalised, we employ a quick and dirty normalisation based on the
        threshold: that is, we divide the positional-cost by the gating threshold, thus ensuring
        that the valid values range 0-1.
        Note also that the authors work with the squared distance. I also sqrt this, so that it
        is more intuitive in terms of values.
        """
        # Compute First the Position-based Cost Matrix
        pos_cost = np.empty([len(track_indices), len(detection_indices)])
        msrs = np.asarray([dets[i].to_xyah() for i in detection_indices])
        for row, track_idx in enumerate(track_indices):
            pos_cost[row, :] = np.sqrt(
                self.kf.gating_distance(
                    tracks[track_idx].mean, tracks[track_idx].covariance, msrs, False
                )
            ) / self.GATING_THRESHOLD
        pos_gate = pos_cost > 1.0
        # Now Compute the Appearance-based Cost Matrix
        app_cost = self.metric.distance(
            np.array([dets[i].feature for i in detection_indices]),
            np.array([tracks[i].track_id for i in track_indices]),
        )
        app_gate = app_cost > self.metric.matching_threshold
        # Now combine and threshold
        cost_matrix = self._lambda * pos_cost + (1 - self._lambda) * app_cost
        cost_matrix[np.logical_or(pos_gate, app_gate)] = linear_assignment.INFTY_COST
        # Return Matrix
        return cost_matrix

    def _match(self, detections):
        # Split track set into confirmed and unconfirmed tracks.
        confirmed_tracks = [i for i, t in enumerate(self.tracks) if t.is_confirmed()]
        unconfirmed_tracks = [i for i, t in enumerate(self.tracks) if not t.is_confirmed()]

        # Associate confirmed tracks using appearance features.
        matches_a, unmatched_tracks_a, unmatched_detections = linear_assignment.matching_cascade(
            self._full_cost_metric,
            linear_assignment.INFTY_COST - 1,  # no need for self.metric.matching_threshold here,
            self.max_age,
            self.tracks,
            detections,
            confirmed_tracks,
        )

        # Associate remaining tracks together with unconfirmed tracks using IOU.
        iou_track_candidates = unconfirmed_tracks + [
            k for k in unmatched_tracks_a if self.tracks[k].time_since_update == 1
        ]
        unmatched_tracks_a = [
            k for k in unmatched_tracks_a if self.tracks[k].time_since_update != 1
        ]
        matches_b, unmatched_tracks_b, unmatched_detections = linear_assignment.min_cost_matching(
            iou_matching.iou_cost,
            self.max_iou_distance,
            self.tracks,
            detections,
            iou_track_candidates,
            unmatched_detections,
        )

        matches = matches_a + matches_b
        unmatched_tracks = list(set(unmatched_tracks_a + unmatched_tracks_b))
        return matches, unmatched_tracks, unmatched_detections

    def _initiate_track(self, detection, class_id, conf, n_parked_cars):
        mean, covariance = self.kf.initiate(detection.to_xyah())
        self.tracks.append(Track( # Car tracks
                                mean, covariance, self._next_id, class_id, conf, self.n_init, self.max_age,
                                detection.feature))
        self._next_id += 1

