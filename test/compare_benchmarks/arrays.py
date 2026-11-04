n = 2048
steps = 80
target = [(i % 97) * 0.02 - 1.0 for i in range(n)]
pos = [0.0] * n
vel = [0.0] * n
dt = 0.01
for _ in range(steps):
    for i in range(n):
        force = (target[i] - pos[i]) * 4.0 - vel[i] * 0.5
        vel[i] += force * dt
        pos[i] += vel[i] * dt
print(int(sum(pos) * 1_000_000_000), int(sum(v * v for v in vel) * 1_000_000_000))
