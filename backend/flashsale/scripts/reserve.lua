
local replay = redis.call('HGET', KEYS[3], ARGV[2])
if replay then return {2, replay} end

local held = redis.call('HGET', KEYS[2], ARGV[1])
if held then return {3, held} end

local stock = tonumber(redis.call('GET', KEYS[1]))
if (not stock) or stock <= 0 then return {0, ''} end

redis.call('DECR', KEYS[1])
redis.call('HSET', KEYS[2], ARGV[1], ARGV[3])
redis.call('HSET', KEYS[3], ARGV[2], ARGV[3])
redis.call('HSET', KEYS[6], 'status', 'PROCESSING', 'user', ARGV[1], 'idem', ARGV[2],
           'created', ARGV[4], 'expires', ARGV[5], 'released', '0')
redis.call('ZADD', KEYS[5], tonumber(ARGV[5]), ARGV[3])
redis.call('RPUSH', KEYS[4], ARGV[6])
return {1, ARGV[3]}
