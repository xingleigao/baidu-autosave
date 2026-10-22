"""根据同一分享内的季数证据生成保守的旧季排除规则。"""

import posixpath
import re
import unicodedata


SEASON_PATTERN = re.compile(
    r'(?<![a-z0-9])(?:s(?:eason)?[ ._-]*([0-9]{1,3})(?![0-9])|'
    r'第\s*([零〇一二三四五六七八九十百两0-9]{1,6})\s*季)', re.IGNORECASE)
RANGE_PATTERN = re.compile(r'^\s*[-~～至到、,，&+]\s*(?:s(?:eason)?\s*)?\d', re.IGNORECASE)


def season_number(value):
    if value.isdigit():
        return int(value)
    digits = dict(zip('零〇一二三四五六七八九两', (0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 2)))
    total = current = 0
    for character in value:
        if character in digits:
            current = digits[character]
        else:
            total += (current or 1) * {'十': 10, '百': 100}[character]
            current = 0
    return total + current


def season_identity(path):
    components = path.strip('/').split('/')
    file_season = None
    for index in range(len(components) - 1, -1, -1):
        component = unicodedata.normalize('NFKC', components[index])
        matches = list(SEASON_PATTERN.finditer(component))
        if not matches:
            continue
        if len(matches) != 1 or RANGE_PATTERN.search(component[matches[0].end():]):
            return None
        match = matches[0]
        number = season_number(match.group(1) or match.group(2))
        if number <= 0:
            return None
        if file_season is not None and file_season[1] != number:
            return None
        title = re.sub(r'[\W_]+', '', component[:match.start()].casefold())
        if not title:
            parent = '/'.join(components[:index])
            if not parent:
                return None
            title = re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', parent).casefold())
        if not title:
            return None
        identity = title, number
        if index == len(components) - 1:
            file_season = identity
            continue
        return identity
    return file_season


def build_season_rule(entries, video_extensions):
    seasons = {}
    identities = {}
    for entry in entries:
        if entry.get('is_dir') or entry.get('isdir'):
            continue
        path = entry.get('path') or entry.get('server_filename', '')
        if posixpath.splitext(path)[1].lower() not in video_extensions:
            continue
        identity = season_identity(path)
        if identity:
            identities[path] = identity
            title, number = identity
            seasons.setdefault(title, set()).add(number)
    groups = [{'title': title, 'keep': max(numbers), 'exclude': sorted(numbers - {max(numbers)})}
              for title, numbers in sorted(seasons.items()) if len(numbers) > 1]
    excluded = []
    for entry in entries:
        path = entry.get('path') or entry.get('server_filename', '')
        identity = identities.get(path) or season_identity(path)
        if identity and identity[0] in seasons and identity[1] < max(seasons[identity[0]]):
            excluded.append(path)
    excluded = sorted(set(excluded))
    all_paths = {entry.get('path') or entry.get('server_filename', '') for entry in entries}
    retained = all_paths - set(excluded)
    prefixes = set()
    exact_paths = []
    for path in excluded:
        directory = posixpath.dirname(path)
        prefix = None
        while directory and directory != '/':
            candidate = directory.rstrip('/') + '/'
            if any(other.startswith(candidate) for other in retained):
                break
            prefix = candidate
            directory = posixpath.dirname(directory)
        if prefix:
            prefixes.add(prefix)
        else:
            exact_paths.append(path)
    alternatives = '|'.join([re.escape(prefix.lstrip('/')) for prefix in sorted(prefixes)]
                            + [re.escape(path.lstrip('/')) + '$' for path in exact_paths])
    pattern = rf'^(?!/?(?:{alternatives}))[\s\S]*$' if excluded else ''
    return {'groups': groups, 'excluded_paths': excluded, 'regex_pattern': pattern,
            'description': '；'.join(
                f'{group["title"]}：保留 S{group["keep"]:02d}，排除 '
                + '、'.join(f'S{number:02d}' for number in group['exclude']) for group in groups)
            or '未发现同名多季，不排除旧季（不按年份判断新旧）'}
