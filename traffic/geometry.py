"""폴리라인의 호 길이로 시뮬레이션 위치와 화면 각도를 연결한다."""
from bisect import bisect_right
from itertools import accumulate, pairwise
from math import atan2, hypot


class Path:
    def __init__(self, points):
        self.points = points
        self.distances = [0.0, *accumulate(hypot(x2 - x1, y2 - y1)
                                          for (x1, y1), (x2, y2) in pairwise(points))]
        self.length = self.distances[-1]
        if any(a == b for a, b in pairwise(self.distances)):
            raise ValueError("경로에 같은 좌표가 연속으로 있습니다.")

    def at(self, distance):
        distance = max(0.0, min(self.length, distance))
        index = min(len(self.points) - 2, bisect_right(self.distances, distance) - 1)
        x, y = self.points[index]
        end_x, end_y = self.points[index + 1]
        fraction = (distance - self.distances[index]) / (self.distances[index + 1] - self.distances[index])
        dx, dy = end_x - x, end_y - y
        return x + fraction * dx, y + fraction * dy, atan2(dy, dx)
