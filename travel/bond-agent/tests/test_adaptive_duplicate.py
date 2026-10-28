from zippie.datapath import PathState, Scheduler, SendMode


def scheduler():
    result = Scheduler(duplicate_fanout=2)
    for path_id in range(4):
        result.add_path(PathState(path_id, str(path_id)))
    return result


def test_one_bad_leg_never_widens():
    s = scheduler()
    for _ in range(4):
        s.update_duplicate_loss({0: 30, 1: 0, 2: 0, 3: 0}, 20)
    assert s.select(SendMode.DUPLICATE) == [0, 1]


def test_all_bad_widens_and_hysteresis_narrows_on_recovery():
    s = scheduler()
    for _ in range(3):
        s.update_duplicate_loss(dict.fromkeys(range(4), 30), 20)
    assert s.select(SendMode.DUPLICATE) == [0, 1, 2, 3]
    s.update_duplicate_loss(dict.fromkeys(range(4), 18), 20)
    assert s.select(SendMode.DUPLICATE) == [0, 1, 2, 3]
    for _ in range(3):
        s.update_duplicate_loss(dict.fromkeys(range(4), 5), 20)
    assert s.select(SendMode.DUPLICATE) == [0, 1]


def test_unknown_loss_and_new_carrying_leg_cannot_widen():
    s = scheduler()
    for _ in range(3):
        s.update_duplicate_loss({0: 30, 1: 30, 2: 30}, 20)
    assert s.select(SendMode.DUPLICATE) == [0, 1]


def test_zero_weight_leg_is_not_in_carrying_set():
    s = scheduler()
    s.set_weight(3, 0)
    for _ in range(3):
        s.update_duplicate_loss({0: 30, 1: 30, 2: 30, 3: 0}, 20)
    assert s.select(SendMode.DUPLICATE) == [0, 1, 2]


def test_membership_changes_clear_widening_even_between_samples():
    s = scheduler()
    for _ in range(3):
        s.update_duplicate_loss(dict.fromkeys(range(4), 30), 20)
    assert len(s.select(SendMode.DUPLICATE)) == 4
    s.set_weight(0, 0)
    s.set_weight(0, 100)
    assert len(s.select(SendMode.DUPLICATE)) == 2
    for _ in range(3):
        s.update_duplicate_loss(dict.fromkeys(range(4), 30), 20)
    s.set_healthy(0, False)
    s.set_healthy(0, True)
    assert len(s.select(SendMode.DUPLICATE)) == 2
