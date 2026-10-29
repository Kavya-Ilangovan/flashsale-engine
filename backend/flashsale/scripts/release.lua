
if redis.call('EXISTS', KEYS[4]) == 0 then return 0 end
if redis.call('HGET', KEYS[4], 'released') == '1' then return 0 end

local user = redis.call('HGET', KEYS[4], 'user')
redis.call('HSET', KEYS[4], 'status', ARGV[2], 'released', '1')
redis.call('INCR', KEYS[1])
if user and redis.call('HGET', KEYS[2], user) == ARGV[1] then
  redis.call('HDEL', KEYS[2], user)
end
redis.call('ZREM', KEYS[3], ARGV[1])
return 1
