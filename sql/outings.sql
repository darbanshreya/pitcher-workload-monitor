-- Builds one row per pitcher per game, with pitch count, days of rest,
-- rolling workload, and average fastball mechanics for that outing.
--
-- Workload definitions:
--   acute_7d        = pitches thrown in the 7 days ending on this outing
--   chronic_weekly  = pitches thrown in the 28 days ending on this outing, divided by 4
--   acwr            = acute_7d / chronic_weekly
--   appearances_3d  = outings in the 3 days ending on this outing (back to back to back)
--   appearances_7d  = outings in the 7 days ending on this outing
-- The ratio is only treated as reliable once a pitcher has 28 days of history.
-- Workload flags are set in pipeline.py, because they depend on each pitcher's role
-- and his own pitch count range.

DROP TABLE IF EXISTS outings;

CREATE TABLE outings AS
WITH per_game AS (
    SELECT
        p.pitcher_id,
        p.game_pk,
        p.game_date,
        COUNT(*)                                   AS pitches,
        MIN(p.inning)                              AS first_inning,
        SUM(p.pitch_type = pr.primary_fastball)    AS fb_pitches,
        AVG(CASE WHEN p.pitch_type = pr.primary_fastball THEN p.release_speed END)     AS fb_velo,
        AVG(CASE WHEN p.pitch_type = pr.primary_fastball THEN p.release_pos_x END)     AS fb_release_x,
        AVG(CASE WHEN p.pitch_type = pr.primary_fastball THEN p.release_pos_z END)     AS fb_release_z,
        AVG(CASE WHEN p.pitch_type = pr.primary_fastball THEN p.release_extension END) AS fb_extension,
        AVG(CASE WHEN p.pitch_type = pr.primary_fastball THEN p.release_spin_rate END) AS fb_spin
    FROM pitches p
    JOIN pitchers pr ON pr.pitcher_id = p.pitcher_id
    GROUP BY p.pitcher_id, p.game_pk, p.game_date
),
windowed AS (
    SELECT
        g.*,
        julianday(g.game_date) - julianday(LAG(g.game_date) OVER by_game) - 1 AS days_rest_raw,
        SUM(g.pitches) OVER (
            PARTITION BY g.pitcher_id
            ORDER BY julianday(g.game_date)
            RANGE BETWEEN 6 PRECEDING AND CURRENT ROW
        ) AS acute_7d,
        SUM(g.pitches) OVER (
            PARTITION BY g.pitcher_id
            ORDER BY julianday(g.game_date)
            RANGE BETWEEN 27 PRECEDING AND CURRENT ROW
        ) / 4.0 AS chronic_weekly,
        COUNT(*) OVER (
            PARTITION BY g.pitcher_id
            ORDER BY julianday(g.game_date)
            RANGE BETWEEN 2 PRECEDING AND CURRENT ROW
        ) AS appearances_3d,
        COUNT(*) OVER (
            PARTITION BY g.pitcher_id
            ORDER BY julianday(g.game_date)
            RANGE BETWEEN 6 PRECEDING AND CURRENT ROW
        ) AS appearances_7d,
        julianday(g.game_date)
            - julianday(MIN(g.game_date) OVER (PARTITION BY g.pitcher_id)) AS days_since_first,
        ROW_NUMBER() OVER by_game AS outing_number
    FROM per_game g
    WINDOW by_game AS (PARTITION BY g.pitcher_id ORDER BY g.game_date, g.game_pk)
)
SELECT
    pitcher_id,
    game_pk,
    game_date,
    outing_number,
    pitches,
    first_inning,
    -- Second game of a doubleheader would come out as -1, so floor at 0.
    CASE WHEN days_rest_raw IS NULL THEN NULL ELSE MAX(days_rest_raw, 0) END AS days_rest,
    acute_7d,
    ROUND(chronic_weekly, 1)                         AS chronic_weekly,
    ROUND(acute_7d / chronic_weekly, 2)              AS acwr,
    CASE WHEN days_since_first >= 27 THEN 1 ELSE 0 END AS acwr_reliable,
    appearances_3d,
    appearances_7d,
    fb_pitches,
    ROUND(fb_velo, 2)       AS fb_velo,
    ROUND(fb_release_x, 3)  AS fb_release_x,
    ROUND(fb_release_z, 3)  AS fb_release_z,
    ROUND(fb_extension, 2)  AS fb_extension,
    ROUND(fb_spin, 0)       AS fb_spin
FROM windowed
ORDER BY pitcher_id, game_date, game_pk;
