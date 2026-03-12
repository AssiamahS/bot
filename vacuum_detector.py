liq_hist = {}

def get_vacuum_score(coin, book):
    bid, ask = book.get('bid_depth', 0), book.get('ask_depth', 0)
    total = bid + ask
    if total == 0: return 0
    
    depth_ratio = min(bid, ask) / total
    h = liq_hist.setdefault(coin, [])
    h.append(depth_ratio)
    if len(h) > 12: h.pop(0)
    if len(h) < 8: return 0

    recent_avg = sum(h[-4:]) / 4
    older_avg = sum(h[:4]) / 4
    thinning = older_avg - recent_avg
    
    return max(0, thinning * 25) # Normalized score
